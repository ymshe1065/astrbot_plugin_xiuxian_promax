# v49 品阶改名迁移回归测试
import asyncio
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

astrbot = types.ModuleType("astrbot")
api_mod = types.ModuleType("astrbot.api")


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
astrbot.api = api_mod
sys.modules.setdefault("astrbot", astrbot)
sys.modules.setdefault("astrbot.api", api_mod)

import aiosqlite  # noqa: E402

from astrbot_plugin_xiuxian_promax.data import migration  # noqa: E402

SCHEMA = """
CREATE TABLE players (
    user_id TEXT PRIMARY KEY,
    storage_ring_items TEXT,
    overflow_items TEXT,
    equipment_instances TEXT,
    equipped_instances TEXT,
    techniques TEXT,
    weapon TEXT,
    armor TEXT,
    main_technique TEXT
);
CREATE TABLE auction_npc_offers (id INTEGER PRIMARY KEY, item_name TEXT);
CREATE TABLE auction_items (id INTEGER PRIMARY KEY, item_name TEXT);
CREATE TABLE pending_gifts (id INTEGER PRIMARY KEY, item_name TEXT);
CREATE TABLE auction_house_lots (
    id INTEGER PRIMARY KEY, item_name TEXT, item_rank TEXT,
    preview_label TEXT, payload_json TEXT
);
CREATE TABLE shop (shop_id TEXT PRIMARY KEY, current_items TEXT);
"""


class TestRankMigration(unittest.TestCase):
    def test_v49_renames_stored_rank_names(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "test.db"
                async with aiosqlite.connect(path) as conn:
                    await conn.executescript(SCHEMA)
                    await conn.execute(
                        "INSERT INTO players VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            "u1",
                            json.dumps({"黄阶·灵纹戒指": 2, "灵草": 3}, ensure_ascii=False),
                            json.dumps({"玄阶·灵纹手套": 1}, ensure_ascii=False),
                            json.dumps([{"id": 1, "name": "地阶·灵纹戒指"}], ensure_ascii=False),
                            json.dumps([{"id": 3, "name": "天阶·灵纹甲胄"}], ensure_ascii=False),
                            json.dumps(["黄阶功法：基础剑诀"], ensure_ascii=False),
                            "天阶·灵纹手套",
                            "玄阶·灵纹甲胄",
                            "天阶功法：太极剑意",
                        ),
                    )
                    await conn.execute("INSERT INTO auction_npc_offers VALUES (1, '地阶·灵纹戒指')")
                    await conn.execute("INSERT INTO auction_items VALUES (1, '灵植包·天阶')")
                    await conn.execute("INSERT INTO pending_gifts VALUES (1, '珍品·玉佩')")
                    await conn.execute(
                        "INSERT INTO auction_house_lots VALUES (1, '圣品·诛仙剑', '天阶', '天阶·诛仙剑', ?)",
                        (json.dumps({"description": "这是一件天阶级别的武器"}, ensure_ascii=False),),
                    )
                    await conn.execute(
                        "INSERT INTO shop VALUES ('global', ?)",
                        (
                            json.dumps(
                                [{"name": "地阶·灵纹戒指", "rank": "地阶",
                                  "data": {"name": "地阶·灵纹戒指", "rank": "地阶"}}],
                                ensure_ascii=False,
                            ),
                        ),
                    )
                    await conn.commit()

                    await migration._migrate_to_v49(conn, None)

                    row = (await (await conn.execute(
                        "SELECT storage_ring_items, overflow_items, equipment_instances, "
                        "equipped_instances, techniques, weapon, armor, main_technique FROM players"
                    )).fetchone())
                    self.assertEqual(
                        json.loads(row[0]), {"凡品·灵纹戒指": 2, "灵草": 3}
                    )
                    self.assertEqual(json.loads(row[1]), {"黄品·灵纹手套": 1})
                    self.assertEqual(json.loads(row[2])[0]["name"], "玄品·灵纹戒指")
                    self.assertEqual(json.loads(row[3])[0]["name"], "圣品·灵纹甲胄")
                    self.assertEqual(json.loads(row[4]), ["凡品功法：基础剑诀"])
                    self.assertEqual(row[5], "圣品·灵纹手套")
                    self.assertEqual(row[6], "黄品·灵纹甲胄")
                    self.assertEqual(row[7], "圣品功法：太极剑意")

                    npc_offer = await (await conn.execute(
                        "SELECT item_name FROM auction_npc_offers"
                    )).fetchone()
                    self.assertEqual(npc_offer[0], "玄品·灵纹戒指")

                    auction = await (await conn.execute(
                        "SELECT item_name FROM auction_items"
                    )).fetchone()
                    self.assertEqual(auction[0], "灵植包·圣品")

                    gift = await (await conn.execute(
                        "SELECT item_name FROM pending_gifts"
                    )).fetchone()
                    self.assertEqual(gift[0], "地品·玉佩")

                    lot = await (await conn.execute(
                        "SELECT item_name, item_rank, preview_label, payload_json "
                        "FROM auction_house_lots"
                    )).fetchone()
                    self.assertEqual(lot[0], "圣品·诛仙剑")
                    self.assertEqual(lot[1], "圣品")
                    self.assertEqual(lot[2], "圣品·诛仙剑")
                    self.assertIn("圣品", lot[3])

                    shelf = (await (await conn.execute(
                        "SELECT current_items FROM shop"
                    )).fetchone())[0]
                    entries = json.loads(shelf)
                    self.assertEqual(entries[0]["name"], "玄品·灵纹戒指")
                    self.assertEqual(entries[0]["rank"], "玄品")
                    self.assertEqual(entries[0]["data"]["rank"], "玄品")

        asyncio.run(run())

    def test_v49_does_not_touch_required_levels(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "test.db"
                async with aiosqlite.connect(path) as conn:
                    await conn.executescript(SCHEMA)
                    await conn.execute(
                        "INSERT INTO players VALUES ('u1','{}','{}','[]','[]','[]','','','')"
                    )
                    await conn.commit()
                    await migration._migrate_to_v49(conn, None)
                    row = await (await conn.execute(
                        "SELECT storage_ring_items FROM players WHERE user_id='u1'"
                    )).fetchone()
                    self.assertEqual(row[0], "{}")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
