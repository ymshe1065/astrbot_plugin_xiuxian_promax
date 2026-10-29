import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FARM_HERBS = {
    "灵草": "凡品",
    "血灵草": "凡品",
    "冰心草": "黄品",
    "火焰花": "黄品",
    "九叶灵芝": "玄品",
    "幽冥草": "圣品",
    "冰火双生花": "圣品",
    "血玉灵芝": "圣品",
    "九幽仙芝": "圣品",
}


class TestFarmAlchemyLinks(unittest.TestCase):
    def test_farm_herbs_exist_in_material_catalog(self):
        data = json.loads((ROOT / "config" / "material_catalog.json").read_text(encoding="utf-8"))
        catalog = {item["name"]: item for item in data["materials"]}
        for name, tier in FARM_HERBS.items():
            self.assertIn(name, catalog)
            self.assertEqual(catalog[name]["category"], "灵植")
            self.assertEqual(catalog[name]["tier"], tier)

    def test_farm_herbs_exist_in_item_config(self):
        data = json.loads((ROOT / "config" / "items.json").read_text(encoding="utf-8"))
        names = {item.get("name") for item in data.values() if isinstance(item, dict)}
        for name in FARM_HERBS:
            self.assertIn(name, names)


if __name__ == "__main__":
    unittest.main()
