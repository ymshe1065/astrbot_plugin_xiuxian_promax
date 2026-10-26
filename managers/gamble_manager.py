# managers/gamble_manager.py
import json
import random
import time
from typing import Tuple
from ..data import DataBase
from ..models import Player

class GambleManager:
    """赌石与黑市管理器"""
    def __init__(self, db: DataBase, config_manager, storage_ring_mgr):
        self.db = db
        self.config_manager = config_manager
        self.storage_ring_mgr = storage_ring_mgr

    async def cut_stone(self, player: Player, count: int) -> Tuple[bool, str]:
        """切原石"""
        if count <= 0 or count > 10:
            return False, "❌ 一次最多切 10 块原石。"
            
        total_cost = 10000 * count
        if player.gold < total_cost:
            return False, f"❌ 灵石不足！切 {count} 块原石需要 {total_cost:,} 灵石。"

        player.gold -= total_cost
        
        # ===== 修复点：动态获取可用物品池，防止出现配置里没有的“幽灵物品” =====
        all_items = list(self.config_manager.items_data.values())
        
        def get_random_by_rank(rank):
            pool = [i for i in all_items if i.get("rank") == rank]
            return random.choice(pool)["name"] if pool else "精铁"

        results = []
        for _ in range(count):
            roll = random.random()
            if roll < 0.80:
                # 80% 废料 (黄阶)
                item_name = get_random_by_rank("黄阶")
                results.append(f"切出了【{item_name}】（废料）")
                await self.storage_ring_mgr.store_item(player, item_name, 5, silent=True)
            elif roll < 0.95:
                # 15% 稀有材料 (玄阶)
                item_name = get_random_by_rank("玄阶")
                results.append(f"✨ 切出了【{item_name}】！")
                await self.storage_ring_mgr.store_item(player, item_name, 1, silent=True)
            elif roll < 0.999:
                # 4.9% 地阶/天阶法宝
                rank_pool = "天阶" if random.random() < 0.2 else "地阶"
                item_name = get_random_by_rank(rank_pool)
                results.append(f"🎉 金光四射！切出了【{item_name}】！")
                await self.storage_ring_mgr.store_item(player, item_name, 1, silent=True)
            else:
                # 0.1% 极其稀有的彩蛋
                results.append("🌌 天地异象！你切出了传说中的【护府令】x3！")
                await self.storage_ring_mgr.store_item(player, "护府令", 3, silent=True)

        await self.db.update_player(player)
        
        msg = [
            f"⛏️ 你花费了 {total_cost:,} 灵石，切开了 {count} 块原石：",
            "━━━━━━━━━━━━━━━",
            "\n".join(results),
            "━━━━━━━━━━━━━━━",
            f"💰 剩余灵石：{player.gold:,}"
        ]
        return True, "\n".join(msg)

    BLACK_MARKET_FAKES = [
        {"name": "神秘的葫芦", "price": 5000, "desc": "不知道装了什么。", "fake": True},
        {"name": "劣质回气散", "price": 200, "desc": "闻起来有点刺鼻。", "fake": True},
    ]
    BLACK_MARKET_RANK_WEIGHTS = {"玄阶": 6, "地阶": 3, "天阶": 1}

    def _black_market_type_label(self, data: dict) -> str:
        item_type = data.get("type", "")
        if item_type == "法器":
            return data.get("subtype") or "法器"
        return {
            "丹药": "丹药",
            "功法": "功法",
            "符箓": "符箓",
            "材料": "材料",
            "杂物": "杂物",
        }.get(item_type, item_type or "物品")

    def _black_market_effect(self, data: dict) -> str:
        desc = data.get("description") or ""
        if desc:
            return desc[:50] + ("..." if len(desc) > 50 else "")
        attrs = []
        for key, label in (
            ("physical_damage", "物伤"),
            ("magic_damage", "法伤"),
            ("physical_defense", "物防"),
            ("magic_defense", "法防"),
            ("mental_power", "精神力"),
        ):
            if data.get(key, 0):
                attrs.append(f"{label}+{data[key]}")
        return "、".join(attrs[:4])

    def _get_black_market_items(self):
        """按小时生成黑市商品；同一小时展示与购买一致。"""
        now = int(time.time())
        hour_key = now // 3600
        rng = random.Random(hour_key)

        real_pool = {}
        for source in (self.config_manager.items_data, self.config_manager.weapons_data):
            for name, data in source.items():
                if not isinstance(data, dict):
                    continue
                if int(data.get("price", 0) or 0) <= 0:
                    continue
                if data.get("rank") not in self.BLACK_MARKET_RANK_WEIGHTS:
                    continue
                if data.get("type") == "储物戒":
                    continue
                real_name = data.get("name") or name
                real_pool[real_name] = data

        candidates = list(real_pool)
        selected = []
        while candidates and len(selected) < 3:
            weights = [
                self.BLACK_MARKET_RANK_WEIGHTS.get(
                    real_pool[name].get("rank", ""), 1
                )
                for name in candidates
            ]
            pick = rng.choices(candidates, weights=weights, k=1)[0]
            selected.append(pick)
            candidates.remove(pick)

        items = []
        for name in selected:
            data = real_pool[name]
            base_price = max(1, int(data.get("price", 0) or 0))
            price = max(1, int(base_price * 0.9))
            items.append({
                "name": name,
                "price": price,
                "fake": False,
                "data": data,
                "rank": data.get("rank", ""),
                "type": self._black_market_type_label(data),
                "desc": self._black_market_effect(data),
            })
        items.extend(dict(fake) for fake in self.BLACK_MARKET_FAKES)
        if len(items) > 3:
            items = rng.sample(items, 3)

        next_refresh = (hour_key + 1) * 3600
        return items, next_refresh

    LEGACY_GHOST_ITEMS = {
        "天阶·混元道果": 100000,
        "地阶·洗髓丹": 1000,
        "天阶·诛仙剑": 50000,
    }

    async def cleanup_legacy_black_market_items(self) -> int:
        """清理旧黑市产生的幽灵物品并按旧价退款；只执行一次。"""
        flag_key = "black_market_ghost_cleanup_v1"
        if await self.db.ext.get_system_config(flag_key) == "1":
            return 0

        async with self.db.conn.execute(
            "SELECT user_id, storage_ring_items FROM players"
        ) as cursor:
            rows = await cursor.fetchall()

        total_refund = 0
        for row in rows:
            user_id, raw = row[0], row[1]
            try:
                items = json.loads(raw) if raw else {}
            except (TypeError, ValueError):
                continue
            if not isinstance(items, dict):
                continue

            refund = 0
            for name, price in self.LEGACY_GHOST_ITEMS.items():
                count = int(items.get(name, 0) or 0)
                if count > 0:
                    refund += count * price
                    del items[name]
            if refund > 0:
                await self.db.conn.execute(
                    "UPDATE players SET gold = gold + ?, storage_ring_items = ? "
                    "WHERE user_id = ?",
                    (refund, json.dumps(items, ensure_ascii=False), user_id),
                )
                total_refund += refund

        await self.db.ext.set_system_config(flag_key, "1")
        await self.db.conn.commit()
        return total_refund

    def get_black_market(self) -> Tuple[bool, str]:
        """获取当前小时的黑市商品。"""
        items, next_refresh = self._get_black_market_items()

        msg = ["🕶️ 黑市（每小时刷新）", "━━━━━━━━━━━━━━━"]
        for i, item in enumerate(items, 1):
            if item.get("fake"):
                msg.append(f"[{i}] {item['name']} - {item['price']:,} 灵石")
                msg.append(f"    {item.get('desc', '风险品')}")
            else:
                msg.append(
                    f"[{i}] [{item.get('rank', '凡品')}] {item['name']}"
                    f"（{item.get('type', '物品')}） - {item['price']:,} 灵石"
                )
                if item.get("desc"):
                    msg.append(f"    效果：{item['desc']}")

        remaining = max(0, next_refresh - int(time.time()))
        msg.append("━━━━━━━━━━━━━━━")
        msg.append(f"⏳ 下次刷新：{remaining // 60} 分 {remaining % 60} 秒后")
        msg.append("💡 使用 `/黑市购买 <编号>`，真假自负！")
        return True, "\n".join(msg)

    async def buy_black_market(self, player: Player, index: int) -> Tuple[bool, str]:
        """购买当前小时的黑市商品。"""
        items, _ = self._get_black_market_items()

        if index < 1 or index > len(items):
            return False, "❌ 无效的商品编号。"

        target = items[index - 1]
        if player.gold < target["price"]:
            return False, f"❌ 灵石不足！需要 {target['price']:,} 灵石。"

        if target.get("fake"):
            player.gold -= target["price"]
            penalty_hp = int(player.hp * 0.2) if player.hp > 0 else 0
            player.hp = max(1, player.hp - penalty_hp)
            await self.db.update_player(player)
            return True, (
                f"🤢 你买下了【{target['name']}】，结果发现是个假货！\n"
                f"你被毒气所伤，损失了 {penalty_hp} 点气血。"
            )

        data = target.get("data")
        if not data:
            return False, "❌ 商品信息异常，本次交易已取消。"

        # 丹药进入丹药背包，其余进入储物戒
        is_pill = False
        try:
            is_pill = self.config_manager.is_pill(target["name"]) or data.get("type") == "丹药"
        except Exception:
            is_pill = data.get("type") == "丹药"

        player.gold -= target["price"]
        await self.db.update_player(player)

        if is_pill:
            inventory = player.get_pills_inventory()
            inventory[target["name"]] = inventory.get(target["name"], 0) + 1
            player.set_pills_inventory(inventory)
            await self.db.update_player(player)
            return True, (
                f"✅ 交易成功！你获得了【{target['name']}】x1，已放入丹药背包。\n"
                f"消耗灵石：{target['price']:,}"
            )

        ok, store_msg = await self.storage_ring_mgr.store_item(
            player, target["name"], 1, silent=True
        )
        if not ok:
            # 储物戒满/类型不可存：退回灵石，绝不发幽灵物品
            player.gold += target["price"]
            await self.db.update_player(player)
            return False, (
                f"❌ 购买【{target['name']}】失败：{store_msg}\n"
                f"灵石已退回：{target['price']:,}"
            )

        return True, (
            f"✅ 交易成功！你获得了【{target['name']}】x1，已存入储物戒。\n"
            f"消耗灵石：{target['price']:,}"
        )