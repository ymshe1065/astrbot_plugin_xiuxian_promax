# tests/test_invest_hint.py
# 仙市风声：33% 纯掷骰（无连续平静保底）、/仙市 面板直接可见下盘风声
import os
import sys
import tempfile
import types
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


FIXED_SECRET = "unit-test-secret-hint"


class TestInvestHintConfig(unittest.TestCase):
    def test_news_chance_is_stable(self):
        """风声掷骰概率 33%；连续平静保底已移除，不再有强制真消息。"""
        mod = _imp("managers.invest_manager")
        self.assertEqual(mod.NEWS_CHANCE, 0.33)
        self.assertFalse(hasattr(mod, "CALM_STREAK_FORCE"), "连续平静保底应已删除")

    def test_hint_lines_show_calm(self):
        """盘面平静也要显示，否则玩家以为没风声。"""
        handlers = _imp("handlers.invest_handlers")
        lines = handlers._hint_lines("下盘风声 · 10:00", {"pattern": "calm", "title": "今日盘面平静，各铺如常开张。", "code": ""})
        text = "\n".join(lines)
        self.assertIn("下盘风声 · 10:00", text)
        self.assertIn("盘面平静", text)

    def test_hint_lines_market_news_prefix(self):
        handlers = _imp("handlers.invest_handlers")
        lines = handlers._hint_lines("下盘风声", {"pattern": "single", "title": "新矿脉现世", "code": "*", "detail": "各铺生意兴隆。"})
        text = "\n".join(lines)
        self.assertIn("全市场 · 新矿脉现世", text)
        self.assertIn("各铺生意兴隆。", text)


class TestInvestHintRoll(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        self.DataBase = _imp("data.data_manager").DataBase
        self.DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        self.InvestManager = _imp("managers.invest_manager").InvestManager

        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await migration._create_all_tables_v2(self.conn)
        await migration._ensure_latest_columns(self.conn)

        self.db = self.DataBase(self.db_path)
        self.db.conn = self.conn
        self.db.ext = self.DatabaseExtended(self.conn)

        await self.db.ext.set_system_config("invest_secret", FIXED_SECRET)
        self.manager = self.InvestManager(self.db, {}, None)
        self.manager._secret = FIXED_SECRET
        await self.manager._seed_products()

        # 固定一个盘次，前两盘留空由各用例自己铺
        self.day = 100
        self.slot = 5

    async def asyncTearDown(self):
        await self.db.close()
        os.remove(self.db_path)

    async def _insert_event(self, day, slot, pattern):
        await self.conn.execute(
            "INSERT INTO invest_events "
            "(trade_day, slot, code, pattern, direction, base_bias, strength, title, detail) "
            "VALUES (?, ?, '', ?, 0, 0, 1, '测试', '')",
            (day, slot, pattern),
        )
        await self.conn.commit()

    async def _pattern(self, day, slot):
        async with self.conn.execute(
            "SELECT pattern FROM invest_events WHERE trade_day = ? AND slot = ?", (day, slot)
        ) as cur:
            row = await cur.fetchone()
        return row["pattern"] if row else None

    async def test_calm_is_pure_roll(self):
        """去掉保底后连出平静不会被强制打断：平静占比仍由 33% 掷骰决定。"""
        calm, total, two_in_a_row = 0, 48, 0
        prev_calm = False
        for i in range(total):
            tick = self.day * 16 + self.slot + i
            day, s = divmod(tick, 16)
            await self.manager._ensure_news(day, s)
            is_calm = await self._pattern(day, s) == "calm"
            calm += int(is_calm)
            if is_calm and prev_calm:
                two_in_a_row += 1
            prev_calm = is_calm
        ratio = calm / total
        self.assertGreater(ratio, 0.5, f"平静占比过低 {ratio:.2f}，风声太密")
        self.assertLess(ratio, 0.85, f"平静占比过高 {ratio:.2f}，风声太稀")
        # 保底存在时绝不可能出现两连平静；这里至少要能看到
        self.assertGreater(two_in_a_row, 0, "未出现连续两盘平静，疑似保底仍在生效")

    async def test_existing_record_not_overwritten(self):
        """盘中重复调用只保留第一条，不会因为重新掷骰改掉已推送的风声。"""
        await self._insert_event(self.day, self.slot, "calm")
        await self.manager._ensure_news(self.day, self.slot)
        self.assertEqual(await self._pattern(self.day, self.slot), "calm")

    async def test_next_hint_prepares_next_tick(self):
        hint = await self.manager._next_hint(self.manager.tick_time(
            self.day * 16 + self.slot
        ))
        self.assertIsNotNone(hint)
        slot_hours = _imp("managers.invest_manager").SLOT_HOURS
        self.assertEqual(int(hint["hour"]), slot_hours[self.slot + 1])
        # 幂等：再取一次是同一条，不会生成第二条
        again = await self.manager._next_hint(self.manager.tick_time(
            self.day * 16 + self.slot
        ))
        self.assertEqual(hint["title"], again["title"])
        count = await self.manager._scalar(
            "SELECT COUNT(*) FROM invest_events WHERE trade_day = ? AND slot = ?",
            (self.day, self.slot + 1),
        )
        self.assertEqual(count, 1)

    async def test_overview_has_next_hint(self):
        data = await self.manager.get_overview(self.manager.tick_time(self.day * 16 + self.slot))
        self.assertIn("next_hint", data)
        self.assertIsNotNone(data["next_hint"])


if __name__ == "__main__":
    unittest.main()
