# core/item_instance_manager.py
"""轻量装备实例管理：同名装备按实例 ID 区分。"""

from typing import Dict, List, Optional

from ..models import Player
from ..data import DataBase
from .item_rules import ItemRules

__all__ = ["ItemInstanceManager"]


class ItemInstanceManager:
    QUALITY_MULTIPLIER = {"一般": 1.0, "良好": 1.15, "极佳": 1.35, "大师": 1.6}

    def __init__(self, db: DataBase = None, config_manager=None):
        self.db = db
        self.config_manager = config_manager

    def load(self, player: Player) -> List[dict]:
        return [x for x in player.get_equipment_instances() if isinstance(x, dict)]

    def save(self, player: Player, instances: List[dict]):
        player.set_equipment_instances(instances)

    def next_id(self, instances: List[dict]) -> int:
        max_id = 0
        for item in instances:
            try:
                max_id = max(max_id, int(item.get("id", 0) or 0))
            except (TypeError, ValueError):
                continue
        return max_id + 1

    def item_type(self, name: str) -> str:
        if not self.config_manager:
            return ""
        cfg = (getattr(self.config_manager, "items_data", {}) or {}).get(name)
        if not cfg:
            cfg = (getattr(self.config_manager, "weapons_data", {}) or {}).get(name)
        return ItemRules.normalize_equipment_type(cfg) if isinstance(cfg, dict) else ""

    def create_instances(
        self,
        player: Player,
        name: str,
        count: int = 1,
        quality: str = "一般",
        affixes: Optional[list] = None,
        location: str = "storage",
        slot: str = "",
    ) -> List[dict]:
        instances = self.load(player)
        next_id = self.next_id(instances)
        created = []
        for _ in range(max(1, int(count or 1))):
            inst = {
                "id": next_id,
                "name": name,
                "type": self.item_type(name),
                "quality": quality or "一般",
                "affixes": list(affixes or []),
                "location": location,
                "slot": slot,
            }
            instances.append(inst)
            created.append(inst)
            next_id += 1
        self.save(player, instances)
        return created

    def list_instances(self, player: Player, location: Optional[str] = None, name: Optional[str] = None) -> List[dict]:
        result = []
        for inst in self.load(player):
            if location is not None and inst.get("location") != location:
                continue
            if name is not None and inst.get("name") != name:
                continue
            result.append(inst)
        return result

    def get_instance(self, player: Player, instance_id: int) -> Optional[dict]:
        try:
            instance_id = int(instance_id)
        except (TypeError, ValueError):
            return None
        for inst in self.load(player):
            if int(inst.get("id", 0) or 0) == instance_id:
                return inst
        return None

    def find_for_equip(self, player: Player, name: str, index: int = 1) -> Optional[dict]:
        candidates = self.list_instances(player, "storage", name)
        if not candidates:
            return None
        candidates.sort(key=lambda x: int(x.get("id", 0) or 0))
        idx = max(1, int(index or 1)) - 1
        if idx >= len(candidates):
            return None
        return candidates[idx]

    def count_storage(self, player: Player, name: str) -> int:
        return len(self.list_instances(player, "storage", name))

    def move(self, player: Player, instance_id: int, location: str, slot: str = "") -> bool:
        instances = self.load(player)
        for inst in instances:
            if int(inst.get("id", 0) or 0) == int(instance_id):
                inst["location"] = location
                inst["slot"] = slot if location == "equipped" else ""
                self.save(player, instances)
                return True
        return False

    def remove(self, player: Player, instance_id: int) -> bool:
        instances = self.load(player)
        remaining = [x for x in instances if int(x.get("id", 0) or 0) != int(instance_id)]
        if len(remaining) == len(instances):
            return False
        self.save(player, remaining)
        return True

    def storage_counts(self, player: Player) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for inst in self.list_instances(player, "storage"):
            name = inst.get("name", "")
            counts[name] = counts.get(name, 0) + 1
        return counts

    def quality_multiplier(self, quality: str) -> float:
        return float(self.QUALITY_MULTIPLIER.get(quality or "一般", 1.0))

    def change_owner(self, source: Player, target: Player, instance_id: int, slot: str = "") -> bool:
        inst = self.get_instance(source, instance_id)
        if not inst:
            return False
        data = dict(inst)
        data.pop("id", None)
        data["location"] = "storage"
        data["slot"] = ""
        instances = self.load(target)
        data["id"] = self.next_id(instances)
        instances.append(data)
        self.save(target, instances)
        self.remove(source, instance_id)
        return True
