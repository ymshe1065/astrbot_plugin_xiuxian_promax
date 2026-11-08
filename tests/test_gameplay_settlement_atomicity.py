"""Boss / 团本奖励失败重试与秘境零值配置回归。"""
import asyncio
import copy
import json
import time
import unittest
from unittest.mock import patch

import test_world_boss_raid as fixtures
from test_world_boss_raid import _imp, ROOT


class TestBossSettlementAtomicity(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.TestWorldBossRaid.asyncSetUp(self)
        # 使用正式运行时的任务独立连接，覆盖并发结算真实路径。
        await self.db.close()
        await self.db.connect()
        self.conn = self.db.conn
        self.cm = _imp("config_manager").ConfigManager(ROOT)
        self.ring = _imp("core.storage_ring_manager").StorageRingManager(self.db, self.cm)
        self.world_boss = self.WorldBossManager(self.db, self.cm, self.ring)
        self.raid = self.RaidManager(self.db, self.cm, self.ring, self.world_boss)
        self.world_boss.roll_drop = lambda _: ("灵草", 1)
        self.world_boss.roll_equipment_reward = lambda *a, **kw: None
        self.title_mgr = _imp("managers.title_manager").TitleManager(self.db)
        self.world_boss.title_mgr = self.title_mgr

    asyncTearDown = fixtures.TestWorldBossRaid.asyncTearDown
    _create_player = fixtures.TestWorldBossRaid._create_player

    async def _prepare(self, kind):
        await self._create_player("first", level=20)
        await self._create_player("second", level=20)
        if kind == "boss":
            ok, msg = await self.world_boss.spawn_boss()
            self.assertTrue(ok, msg)
            record = await self.world_boss.get_active_boss()
            table, damage_table, key = "world_boss", "world_boss_damage", "boss_id"
            settle = self.world_boss.settle_boss_kill
        else:
            ok, msg = await self.raid.spawn_raid(difficulty=1)
            self.assertTrue(ok, msg)
            record = await self.raid.get_active_raid()
            table, damage_table, key = "world_boss_raids", "world_boss_raid_damage", "raid_id"
            settle = self.raid.settle_raid
        record_id = record[key]
        await self.conn.execute(f"UPDATE {table} SET status = 2, current_hp = 0 WHERE {key} = ?", (record_id,))
        for uid, damage in (("first", 2000), ("second", 1000)):
            await self.conn.execute(
                f"INSERT INTO {damage_table} ({key}, user_id, total_damage, attack_count, last_attack_time) "
                "VALUES (?, ?, ?, 1, ?)", (record_id, uid, damage, int(time.time())),
            )
        await self.conn.commit()
        return table, key, record_id, settle

    async def _state(self, table, key, record_id):
        async with self.conn.execute(f"SELECT status, settled FROM {table} WHERE {key} = ?", (record_id,)) as cursor:
            return tuple(await cursor.fetchone())

    async def _verify_failure_retry(self, kind, failure):
        table, key, record_id, settle = await self._prepare(kind)
        original_gold = self.db.add_player_gold
        original_grant = self.ring.grant_item

        async def fail_gold(uid, *args, **kwargs):
            if uid == "second":
                raise RuntimeError("模拟第二位玩家发奖失败")
            return await original_gold(uid, *args, **kwargs)

        async def fail_drop(player, *args, **kwargs):
            if player.user_id == "second":
                raise RuntimeError("模拟第二位玩家掉落写入失败")
            return await original_grant(player, *args, **kwargs)

        obj, name, replacement = (
            (self.db, "add_player_gold", fail_gold) if failure == "gold"
            else (self.ring, "grant_item", fail_drop)
        )
        with patch.object(obj, name, replacement):
            with self.assertRaisesRegex(RuntimeError, "模拟第二位"):
                await settle(record_id, "first")
        self.assertEqual(await self._state(table, key, record_id), (2, 0))
        for uid in ("first", "second"):
            player = await self.db.get_player_by_id(uid)
            self.assertEqual(player.gold, 0)
            self.assertEqual(player.get_storage_ring_items(), {})
            self.assertFalse(self.title_mgr.has_title(player, "灭世魔神"))

        # 失败后两个独立任务同时补偿结算：只允许一份奖励成功。
        results = await asyncio.gather(settle(record_id, "first"), settle(record_id, "first"))
        self.assertEqual(sum(bool(result) for result in results), 1)
        for uid in ("first", "second"):
            player = await self.db.get_player_by_id(uid)
            self.assertGreater(player.gold, 0)
            self.assertGreater(player.get_storage_ring_items().get("灵草", 0), 0)
            gold, items = player.gold, player.storage_ring_items
            self.assertEqual(await settle(record_id, "first"), "")
            fresh = await self.db.get_player_by_id(uid)
            self.assertEqual((fresh.gold, fresh.storage_ring_items), (gold, items))
        if kind == "boss":
            self.assertTrue(self.title_mgr.has_title(await self.db.get_player_by_id("first"), "灭世魔神"))

    async def test_boss_gold_failure_rolls_back_and_retries_once(self):
        await self._verify_failure_retry("boss", "gold")

    async def test_boss_drop_failure_rolls_back_and_retries_once(self):
        await self._verify_failure_retry("boss", "drop")

    async def test_raid_gold_failure_rolls_back_and_retries_once(self):
        await self._verify_failure_retry("raid", "gold")

    async def test_raid_drop_failure_rolls_back_and_retries_once(self):
        await self._verify_failure_retry("raid", "drop")

    async def test_alive_boss_cannot_be_settled(self):
        await self._create_player("first", level=20)
        await self.world_boss.spawn_boss()
        boss = await self.world_boss.get_active_boss()
        self.assertEqual(await self.world_boss.settle_boss_kill(boss["boss_id"], "first"), "")
        self.assertEqual(await self._state("world_boss", "boss_id", boss["boss_id"]), (1, 0))


class TestRiftZeroConfiguration(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = _imp("managers.rift_manager").RiftManager(None)
        self.Player = _imp("models").Player
        self.Rift = _imp("models_extended").Rift

    def test_zero_item_chance_survives_config_sync(self):
        self.manager.rifts = copy.deepcopy(self.manager.rifts[:1])
        self.manager.rifts[0]["difficulties"][0]["item_chance"] = 0
        row = next(self.manager._iter_rift_rows())
        self.assertEqual(json.loads(row["rewards"])["item_chance"], 0)

    async def test_zero_item_chance_does_not_roll_materials(self):
        rift = self.Rift(rift_id=1, rift_name="测试", rift_level=1, required_level=0, rewards=json.dumps({
            "item_chance": 0, "equipment_chance": 0,
            "drops": [{"name": "灵草", "weight": 1, "min": 1, "max": 1}],
        }))
        with patch(f"{fixtures.PKG}.managers.rift_manager.random.randint", return_value=1):
            self.assertEqual(await self.manager._roll_rift_drops(self.Player(user_id="u"), rift), [])

    def test_beginner_rift_power_does_not_scale_with_player_level(self):
        rift = self.Rift(rift_id=1, rift_name="测试", rift_level=1, required_level=0)
        self.assertEqual(self.manager._rift_power(rift, self.Player(user_id="u", level_index=35)), 1)


class TestRiftSettlementAtomicity(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await TestBossSettlementAtomicity.asyncSetUp(self)
        self.rift = _imp("managers.rift_manager").RiftManager(self.db, self.cm, self.ring)
        await self.rift.sync_rifts()
        self.player = await fixtures.TestWorldBossRaid._create_player(self, "first", level=20)
        self.player.gold = 100000
        await self.db.update_player(self.player)
        ok, msg = await self.rift.enter_rift("first", 1)
        self.assertTrue(ok, msg)
        cd = await self.db.ext.get_user_cd("first")
        await self.db.ext.set_user_busy("first", cd.type, int(time.time()) - 1, cd.get_extra_data())

    asyncTearDown = fixtures.TestWorldBossRaid.asyncTearDown

    async def test_concurrent_finish_claims_each_exploration_once(self):
        async def drops(*args, **kwargs):
            await asyncio.sleep(0.01)
            return [("灵草", 1)]

        with patch.object(self.rift, "_roll_rift_drops", drops):
            results = await asyncio.gather(
                self.rift.finish_exploration("first"), self.rift.finish_exploration("first")
            )
        self.assertEqual(sum(result[0] for result in results), 1)
        fresh = await self.db.get_player_by_id("first")
        reward = next(result[2] for result in results if result[0])
        self.assertEqual(fresh.gold, self.player.gold + reward["gold"])
        self.assertEqual(fresh.experience, self.player.experience + reward["exp"])
        self.assertEqual(fresh.get_storage_ring_items().get("灵草"), 1)

    async def test_exit_failure_does_not_charge_penalty(self):
        async def fail_free(*args, **kwargs):
            raise RuntimeError("模拟退出状态写入失败")

        with patch.object(self.db.ext, "set_user_free", fail_free):
            with self.assertRaisesRegex(RuntimeError, "模拟退出"):
                await self.rift.exit_rift("first")
        self.assertEqual((await self.db.get_player_by_id("first")).gold, self.player.gold)
        self.assertEqual((await self.db.ext.get_user_cd("first")).type, 3)


class TestWeeklyBossSettlementAtomicity(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await TestBossSettlementAtomicity.asyncSetUp(self)
        self.WeeklyBossManager = _imp("managers.weekly_boss_manager").WeeklyBossManager
        self.weekly = self.WeeklyBossManager(
            self.db, self.cm, self.ring, self.world_boss, self.title_mgr
        )
        self.leader = await fixtures.TestWorldBossRaid._create_player(self, "first", level=22)
        self.member = await fixtures.TestWorldBossRaid._create_player(self, "second", level=22)
        self.leader.set_pills_inventory({"狂暴丹|一般": 3})
        await self.db.update_player(self.leader)
        ok, msg = await self.weekly.create_team(self.leader, "事务测试队")
        self.assertTrue(ok, msg)
        self.cycle = await self.weekly.ensure_current_cycle()
        self.run = await self.weekly._find_member_run(self.cycle["cycle_id"], "first", 0)
        ok, msg = await self.weekly.join_team(self.member, str(self.run["run_id"]))
        self.assertTrue(ok, msg)
        ok, msg = await self.weekly.set_loadout(self.leader, "狂暴丹*3")
        self.assertTrue(ok, msg)
        ok, msg = await self.weekly.prepare_battle(self.leader)
        self.assertTrue(ok, msg)
        self.battle_calls = 0
        self.clear = True
        self.weekly._run_battle = self._battle

    asyncTearDown = fixtures.TestWorldBossRaid.asyncTearDown

    def _battle(self, boss_name, element, hp, members, count):
        self.battle_calls += 1
        for i, member in enumerate(members):
            member["damage"] = 2000 - i * 1000
            member["hp"] = member["max_hp"] // 2
        return {
            "result": "clear" if self.clear else "wipe", "wipe_reason": "测试团灭",
            "rounds": 2, "boss_hp": 0 if self.clear else hp // 2,
            "boss_max_hp": hp, "total_damage": 3000, "members": members,
            "round_summaries": ["测试战报"],
        }

    async def _verify_retry(self, clear, failure):
        self.clear = clear
        original_gold = self.db.add_player_gold
        original_drop = self.ring.grant_item

        async def fail_gold(uid, *args, **kwargs):
            if uid == "second":
                raise RuntimeError("模拟周本灵石发放失败")
            return await original_gold(uid, *args, **kwargs)

        async def fail_drop(player, *args, **kwargs):
            if player.user_id == "second":
                raise RuntimeError("模拟周本掉落发放失败")
            return await original_drop(player, *args, **kwargs)

        obj, name, replacement = (
            (self.db, "add_player_gold", fail_gold) if failure == "gold"
            else (self.ring, "grant_item", fail_drop)
        )
        with patch.object(obj, name, replacement):
            ok, msg = await self.weekly.confirm_battle(self.leader)
        self.assertFalse(ok, msg)
        self.assertIn("再次 /周本确认", msg)
        run = await self.weekly._fetchone("SELECT * FROM weekly_boss_runs WHERE run_id = ?", (self.run["run_id"],))
        self.assertEqual(run["status"], 1)
        self.assertTrue(json.loads(run["log_json"])["pending_settlement"])
        self.assertEqual(await self.weekly._fetchall("SELECT * FROM weekly_boss_claims"), [])
        self.assertEqual(await self.weekly._fetchall("SELECT * FROM weekly_boss_leaderboard"), [])
        state = {}
        for uid in ("first", "second"):
            player = await self.db.get_player_by_id(uid)
            state[uid] = player.hp, player.pills_inventory
            self.assertEqual(player.gold, 0)
            self.assertEqual(player.get_storage_ring_items(), {})
            self.assertFalse(self.title_mgr.has_title(player, "天外镇守者"))
            self.assertEqual(await self.weekly._cycle_attempts_used(self.cycle["cycle_id"], uid), 1)
        self.assertEqual((await self.db.get_player_by_id("first")).get_pills_inventory(), {})

        # 模拟插件重启，恢复不依赖内存预览；非队长确认、取消都不能销毁待发奖励。
        restored = self.WeeklyBossManager(self.db, self.cm, self.ring, self.world_boss, self.title_mgr)
        restored._run_battle = lambda *args: self.fail("补结算不应重新战斗")
        self.assertFalse((await restored.confirm_battle(self.member))[0])
        self.assertFalse((await restored.cancel_battle(self.leader))[0])
        self.assertIn("奖励待结算", await restored.get_status(self.leader))
        results = await asyncio.gather(
            restored.confirm_battle(self.leader), restored.confirm_battle(self.leader)
        )
        self.assertEqual(sum(result[0] for result in results), 1, results)
        for uid in ("first", "second"):
            player = await self.db.get_player_by_id(uid)
            self.assertEqual((player.hp, player.pills_inventory), state[uid])
            self.assertGreater(player.gold, 0)
            self.assertGreater(player.get_storage_ring_items().get("灵草", 0), 0)
            self.assertEqual(await restored._cycle_attempts_used(self.cycle["cycle_id"], uid), 1)
        rows = await restored._fetchall("SELECT clears FROM weekly_boss_leaderboard")
        self.assertEqual([r["clears"] for r in rows], [int(clear), int(clear)])
        self.assertFalse((await restored.confirm_battle(self.leader))[0])
        self.assertEqual(self.battle_calls, 1)

    async def test_clear_gold_failure_retries_after_restart(self):
        await self._verify_retry(True, "gold")

    async def test_clear_drop_failure_retries_after_restart(self):
        await self._verify_retry(True, "drop")

    async def test_wipe_compensation_failure_retries_once(self):
        await self._verify_retry(False, "drop")

    async def test_concurrent_initial_confirmation_starts_only_once(self):
        original = self.weekly._build_member_panel

        async def slower_panel(*args, **kwargs):
            await asyncio.sleep(0.01)
            return await original(*args, **kwargs)

        with patch.object(self.weekly, "_build_member_panel", slower_panel):
            results = await asyncio.gather(
                self.weekly.confirm_battle(self.leader), self.weekly.confirm_battle(self.leader)
            )
        self.assertEqual(sum(result[0] for result in results), 1, results)
        self.assertEqual(self.battle_calls, 1)
        for uid in ("first", "second"):
            self.assertEqual(await self.weekly._cycle_attempts_used(self.cycle["cycle_id"], uid), 1)

    async def test_cancel_preview_prevents_confirmation_without_cost(self):
        self.assertTrue((await self.weekly.cancel_battle(self.leader))[0])
        self.assertFalse((await self.weekly.confirm_battle(self.leader))[0])
        self.assertEqual(self.battle_calls, 0)
        self.assertEqual(await self.weekly._cycle_attempts_used(self.cycle["cycle_id"], "first"), 0)
        self.assertEqual((await self.db.get_player_by_id("first")).get_pills_inventory(), {"狂暴丹|一般": 3})

    async def test_cancel_during_confirmation_prevents_start(self):
        entered, resume = asyncio.Event(), asyncio.Event()
        original = self.weekly._build_member_panel

        async def blocked_panel(*args, **kwargs):
            entered.set()
            await resume.wait()
            return await original(*args, **kwargs)

        with patch.object(self.weekly, "_build_member_panel", blocked_panel):
            confirmation = asyncio.create_task(self.weekly.confirm_battle(self.leader))
            await asyncio.wait_for(entered.wait(), timeout=2)
            self.assertTrue((await self.weekly.cancel_battle(self.leader))[0])
            resume.set()
            ok, msg = await confirmation
        self.assertFalse(ok, msg)
        self.assertEqual(self.battle_calls, 0)
        self.assertEqual(await self.weekly._cycle_attempts_used(self.cycle["cycle_id"], "first"), 0)
        self.assertEqual((await self.db.get_player_by_id("first")).get_pills_inventory(), {"狂暴丹|一般": 3})
