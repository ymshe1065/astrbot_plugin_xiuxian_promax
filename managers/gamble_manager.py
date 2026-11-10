# managers/gamble_manager.py
import json
import random
import time
from typing import Tuple
from ..data import DataBase
from ..models import Player
from ..rank_rules import RANK_NAMES, normalize_rank

class GambleManager:
    """赌石与黑市管理器"""

    # ===== 切原石定价与掉落表 =====
    # 单块原石价：旧版 10000 灵石 / 块，与材料侧价值严重脱节（期望回收仅 24%），
    # 且 80% 的结果是 5 个凡品材料（约 400 灵石），体验极差。改为 5000 / 块。
    STONE_PRICE = 5000
    STONE_MAX_COUNT = 10
    # 装备总概率与「圣品占比」保持不变：玄品 3.92% / 圣品 0.98%
    STONE_GEAR_CHANCE = 0.049
    STONE_SAINT_GEAR_SHARE = 0.20
    STONE_EGG_CHANCE = 0.001
    # 材料掉落：矿材占 80%，其余 20% 为妖兽/灵植/灵液
    STONE_ORE_SHARE = 0.80
    # (品阶, 每块份数, 权重)：品阶越高份数越少、权重越低，形成梯度
    STONE_ORE_TABLE = (("凡品", 6, 42), ("黄品", 4, 32), ("玄品", 2, 23), ("圣品", 1, 3))
    STONE_MATERIAL_TABLE = (("凡品", 4, 42), ("黄品", 2, 32), ("玄品", 1, 23), ("圣品", 1, 3))
    STONE_RANKS = ("凡品", "黄品", "玄品", "圣品")
    STONE_FLAVOR = {
        "圣品装备": ("🌌 天地异象！你切出了传说中的", "！"),
        "玄品装备": ("🎉 金光四射！你切出了", "！"),
        "彩蛋": ("🌠 石中藏宝！你切出了", "！"),
        "圣品": ("🌟 矿脉交辉！你切出了", "！"),
        "玄品": ("✨ 切出了", "（灵矿）"),
        "黄品": ("切出了", "（寻常矿料）"),
        "凡品": ("切出了", "（碎石渣）"),
    }

    def __init__(self, db: DataBase, config_manager, storage_ring_mgr):
        self.db = db
        self.config_manager = config_manager
        self.storage_ring_mgr = storage_ring_mgr
        self._stone_pools = None

    # ===== 掉落池：只收「能存进储物戒」的物品，避免切出后静默丢失 =====
    def _can_store(self, item_name: str) -> bool:
        if not self.storage_ring_mgr:
            return True
        try:
            can_store, _ = self.storage_ring_mgr.can_store_item(item_name)
            return bool(can_store)
        except Exception:
            return False

    def _stone_pool_cache(self):
        """按 (类别, 品阶) 分桶的材料池 + 按品阶分桶的法器池，首次使用时构建。"""
        if self._stone_pools is not None:
            return self._stone_pools
        items = getattr(self.config_manager, "items_data", {}) or {}
        catalog = getattr(self.config_manager, "material_catalog", {}) or {}
        materials, gears = {}, {}
        for name, info in catalog.items():
            item = items.get(name)
            if not isinstance(item, dict) or item.get("type") != "材料":
                continue
            if not self._can_store(name):
                continue
            info = info if isinstance(info, dict) else {}
            category = info.get("category") or ""
            rank = normalize_rank(info.get("tier") or item.get("rank"), "凡品")
            materials.setdefault((category, rank), []).append(name)
        for item in items.values():
            if not isinstance(item, dict) or item.get("type") != "法器":
                continue
            name = item.get("name")
            if not name or not self._can_store(name):
                continue
            rank = normalize_rank(item.get("rank"), "凡品")
            gears.setdefault(rank, []).append(name)
        self._stone_pools = (materials, gears)
        return self._stone_pools

    @classmethod
    def _rank_fallback(cls, rank: str):
        """该品阶无可用物品时，先往下退，再往上找。"""
        ranks = list(cls.STONE_RANKS)
        index = ranks.index(rank) if rank in ranks else 0
        return [ranks[index]] + ranks[:index][::-1] + ranks[index + 1:]

    def _roll_stone_material(self, table, categories, rng) -> dict:
        materials, _ = self._stone_pool_cache()
        ranks = [rank for rank, _, _ in table]
        weights = [weight for _, _, weight in table]
        rank = rng.choices(ranks, weights=weights, k=1)[0]
        qty = next(q for r, q, _ in table if r == rank)
        for candidate in self._rank_fallback(rank):
            pool = []
            for category in categories:
                pool.extend(materials.get((category, candidate), []))
            if pool:
                return {"kind": "material", "rank": candidate, "name": rng.choice(pool), "qty": qty}
        return {"kind": "dust", "rank": rank, "name": None, "qty": 0}

    def _roll_stone_gear(self, rng) -> dict:
        _, gears = self._stone_pool_cache()
        rank = "圣品" if rng.random() < self.STONE_SAINT_GEAR_SHARE else "玄品"
        for candidate in self._rank_fallback(rank):
            pool = gears.get(candidate)
            if pool:
                return {"kind": "gear", "rank": candidate, "name": rng.choice(pool), "qty": 1}
        return {"kind": "dust", "rank": rank, "name": None, "qty": 0}

    def roll_stone_loot(self, rng=None) -> list:
        """开一块原石的掉落；返回列表便于以后扩展成「一块出多件」。"""
        rng = rng or random
        roll = rng.random()
        if roll < self.STONE_GEAR_CHANCE:
            return [self._roll_stone_gear(rng)]
        if roll < self.STONE_GEAR_CHANCE + self.STONE_EGG_CHANCE:
            return [{"kind": "egg", "rank": "", "name": "护府令", "qty": 3}]
        if rng.random() < self.STONE_ORE_SHARE:
            return [self._roll_stone_material(self.STONE_ORE_TABLE, ("矿材",), rng)]
        return [self._roll_stone_material(self.STONE_MATERIAL_TABLE, ("妖兽", "灵植", "灵液"), rng)]

    def _loot_label(self, loot: dict) -> str:
        name, qty = loot.get("name"), int(loot.get("qty") or 0)
        kind, rank = loot.get("kind"), loot.get("rank", "")
        if not name:
            return f"{'碎石渣一堆' if kind == 'dust' else '一团碎屑'}，一无所获（可掉落物品池为空）"
        if kind == "gear":
            prefix, suffix = self.STONE_FLAVOR["圣品装备" if rank == "圣品" else "玄品装备"]
        elif kind == "egg":
            prefix, suffix = self.STONE_FLAVOR["彩蛋"]
        else:
            prefix, suffix = self.STONE_FLAVOR.get(rank, ("切出了", ""))
        return f"{prefix}【{name}】" + (f"x{qty}" if qty > 1 else "") + suffix

    async def _award_loot(self, player: Player, loot: dict) -> str:
        label = self._loot_label(loot)
        name = loot.get("name")
        if not name:
            return label
        success, reason = await self.storage_ring_mgr.store_item(
            player, name, int(loot.get("qty") or 1), silent=True, external_transaction=True
        )
        if success:
            return label
        return f"{label}\n   ⚠️ 未能存入储物戒：{reason}"

    async def cut_stone(self, player: Player, count: int) -> Tuple[bool, str]:
        """切原石"""
        if count <= 0 or count > self.STONE_MAX_COUNT:
            return False, f"❌ 一次最多切 {self.STONE_MAX_COUNT} 块原石。"

        total_cost = self.STONE_PRICE * count
        if player.gold < total_cost:
            return False, f"❌ 灵石不足！切 {count} 块原石需要 {total_cost:,} 灵石。"

        results = []
        # 扣费与所有掉落放入同一事务，避免中途异常时灵石被扣而物品丢失
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            # 事务内重读最新灵石并同步回调用方对象：后续 update_player 全字段回写
            # 不会再拿旧快照覆盖并发入账
            fresh = await self.db.get_player_by_id(player.user_id)
            if not fresh:
                await self.db.conn.rollback()
                return False, "❌ 角色数据异常，请重新踏入仙途。"
            player.gold = fresh.gold

            # 条件原子扣款：余额不足时 rowcount=0，绝不透支
            cursor = await self.db.conn.execute(
                "UPDATE players SET gold = gold - ? WHERE user_id = ? AND gold >= ?",
                (total_cost, player.user_id, total_cost),
            )
            if not cursor.rowcount:
                await self.db.conn.rollback()
                return False, (
                    f"❌ 灵石不足！切 {count} 块原石需要 {total_cost:,} 灵石，"
                    f"你当前只有 {fresh.gold:,}。"
                )
            player.gold -= total_cost

            for _ in range(count):
                for loot in self.roll_stone_loot():
                    results.append(await self._award_loot(player, loot))

            await self.db.update_player(player, commit=False)
            await self.db.conn.commit()
        except Exception:
            await self.db.conn.rollback()
            raise

        msg = [
            f"⛏️ 你花费了 {total_cost:,} 灵石，切开了 {count} 块原石（{self.STONE_PRICE:,}/块）：",
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
    BLACK_MARKET_RANK_WEIGHTS = {
        "黄品": 6, "玄品": 5, "灵品": 4, "地品": 3, "天品": 2, "皇品": 2,
        "圣品": 1, "帝品": 1, "道品": 1, "仙品": 1, "混元先天": 1,
    }
    # 黑市装备品质：整体高于普通货架，大师/极佳更常见
    BLACK_MARKET_QUALITY_WEIGHTS = {"一般": 20, "良好": 35, "极佳": 30, "大师": 15}

    @staticmethod
    def _rank_index(rank: str) -> int:
        try:
            return RANK_NAMES.index(normalize_rank(rank))
        except ValueError:
            return 0

    def _black_market_pick_weight(self, data: dict) -> float:
        """黑市抽取权重：品阶越高、原始权重越低（越稀有）越容易入选。"""
        try:
            base = int(
                data.get("black_market_weight")
                or data.get("drop_weight")
                or data.get("shop_weight")
                or self.BLACK_MARKET_RANK_WEIGHTS.get(data.get("rank", ""), 1)
            )
        except (TypeError, ValueError):
            base = 1
        base = max(1, base)
        index = self._rank_index(data.get("rank", ""))
        return max(1.0, ((1.0 + index) ** 1.5) * (base ** -0.5) * 100)

    def _black_market_type_label(self, data: dict) -> str:
        item_type = data.get("type", "")
        subtype = str(data.get("subtype") or "")
        if item_type == "法器":
            return data.get("subtype") or "法器"
        # 三类丹药共用同一套展示名：修为丹/功能丹靠独立配置的字段识别，
        # 避免装备的 subtype（如“手套”“戒指”）被误判成功能丹。
        if item_type == "exp_pill" or subtype == "exp" or "exp_gain" in data:
            return "修为丹"
        if item_type == "utility_pill" or (
            not item_type and "effect_type" in data
        ):
            return "功能丹"
        if item_type in ("pill", "丹药") or subtype == "breakthrough":
            return "丹药"
        return {
            "丹药": "丹药",
            "功法": "功法",
            "technique": "功法",
            "main_technique": "心法",
            "符箓": "符箓",
            "材料": "材料",
            "杂物": "杂物",
            "weapon": "武器",
            "armor": "防具",
            "head": "头饰",
            "body": "甲胄",
            "boots": "靴子",
            "gloves": "手套",
            "ring": "戒指",
            "amulet": "护符",
        }.get(item_type, item_type or "物品")

    def _black_market_effect(self, data: dict, quality: str = "") -> str:
        from ..core.shop_manager import ShopManager
        return "；".join(ShopManager({}, self.config_manager).get_trade_details({
            "name": data.get("name", "未知物品"), "data": data,
            "price": data.get("price", 0), "rank": data.get("rank", "凡品"),
            "quality": quality,
        }))

    def _get_black_market_items(self):
        """按小时生成黑市商品；同一小时展示与购买一致。"""
        now = int(time.time())
        hour_key = now // 3600
        rng = random.Random(hour_key)

        real_pool = {}
        for source in (
            self.config_manager.items_data,
            self.config_manager.weapons_data,
            getattr(self.config_manager, "techniques_data", {}) or {},
            # 突破丹/修为丹/功能丹独立配置，也必须进入黑市真实商品池，
            # 否则新增丹药只有在丹阁/百宝阁才能买到。
            getattr(self.config_manager, "pills_data", {}) or {},
            getattr(self.config_manager, "exp_pills_data", {}) or {},
            getattr(self.config_manager, "utility_pills_data", {}) or {},
        ):
            for name, data in source.items():
                if not isinstance(data, dict):
                    continue
                if int(data.get("price", 0) or 0) <= 0:
                    continue
                if data.get("rank") not in self.BLACK_MARKET_RANK_WEIGHTS:
                    continue
                if data.get("type") == "储物戒":
                    continue
                # 显式 shop_weight=0 的物品只通过掉落/活动获取（如周本专属天外陨核、
                # 帝品材料、炼丹专属增益丹），不进入黑市；如需黑市专供请配置
                # black_market_weight / drop_weight 覆盖。
                if int(data.get("shop_weight", 1) or 0) == 0 and not (
                    data.get("black_market_weight") or data.get("drop_weight")
                ):
                    continue
                real_name = data.get("name") or name
                real_pool[real_name] = data

        candidates = list(real_pool)
        selected = []
        while candidates and len(selected) < 3:
            weights = [self._black_market_pick_weight(real_pool[name]) for name in candidates]
            pick = rng.choices(candidates, weights=weights, k=1)[0]
            selected.append(pick)
            candidates.remove(pick)

        items = []
        for name in selected:
            data = real_pool[name]
            base_price = max(1, int(data.get("price", 0) or 0))
            # 稀有货溢价：品阶越高，溢价越重
            premium = 1.8 + self._rank_index(data.get("rank", "")) * 0.18 + rng.uniform(0.0, 0.7)
            price = max(1, int(base_price * premium))
            items.append({
                "name": name,
                "price": price,
                "fake": False,
                "data": data,
                "rank": data.get("rank", ""),
                "type": self._black_market_type_label(data),
                "desc": self._black_market_effect(data),
            })
        # 黑市以稀有真品为主，小概率混入一件假货（真假自负）
        if items and rng.random() < 0.2:
            items[rng.randrange(len(items))] = dict(rng.choice(self.BLACK_MARKET_FAKES))

        from ..core.item_rules import ItemRules
        quality_names = list(self.BLACK_MARKET_QUALITY_WEIGHTS)
        quality_weights = [self.BLACK_MARKET_QUALITY_WEIGHTS[q] for q in quality_names]
        for item in items:
            data = item.get("data")
            if item.get("fake") or not isinstance(data, dict):
                continue
            if ItemRules.normalize_equipment_type(data) in ItemRules.GEAR_TYPES:
                item["quality"] = rng.choices(quality_names, weights=quality_weights, k=1)[0]
                item["desc"] = self._black_market_effect(data, item["quality"])

        next_refresh = (hour_key + 1) * 3600
        return items, next_refresh

    LEGACY_GHOST_ITEMS = {
        "圣品·混元道果": 100000,
        "玄品·洗髓丹": 1000,
        "圣品·诛仙剑": 50000,
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

    async def _black_market_purchase_key(self, user_id: str) -> str:
        return f"black_market_bought_{int(time.time()) // 3600}_{user_id}"

    async def _black_market_bought(self, user_id: str) -> set:
        try:
            raw = await self.db.ext.get_system_config(
                await self._black_market_purchase_key(user_id)
            )
            data = json.loads(raw) if raw else []
            return {int(x) for x in data} if isinstance(data, list) else set()
        except Exception:
            return set()

    async def _mark_black_market_bought(self, user_id: str, index: int) -> None:
        bought = await self._black_market_bought(user_id)
        bought.add(int(index))
        await self.db.ext.set_system_config(
            await self._black_market_purchase_key(user_id),
            json.dumps(sorted(bought)),
        )

    async def buy_black_market(self, player: Player, index: int) -> Tuple[bool, str]:
        """购买当前小时的黑市商品（每人每小时每件限购 1 次）。"""
        items, _ = self._get_black_market_items()

        if index < 1 or index > len(items):
            return False, "❌ 无效的商品编号。"

        if index in await self._black_market_bought(player.user_id):
            return False, "❌ 本小时你已经买过这件商品了，请等待下次刷新。"

        target = items[index - 1]
        price = int(target.get("price") or 0)
        if player.gold < price:
            return False, f"❌ 灵石不足！需要 {price:,} 灵石。"

        if target.get("fake"):
            # 假货：扣灵石与扣气血放入同一事务，条件原子扣款防止并发透支
            await self.db.conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self.db.conn.execute(
                    "UPDATE players SET gold = gold - ? WHERE user_id = ? AND gold >= ?",
                    (price, player.user_id, price),
                )
                if not cursor.rowcount:
                    await self.db.conn.rollback()
                    fresh = await self.db.get_player_by_id(player.user_id)
                    have = fresh.gold if fresh else player.gold
                    return False, (
                        f"❌ 灵石不足！需要 {price:,} 灵石，"
                        f"你当前只有 {have:,}。"
                    )
                # 事务内重读最新气血；死亡状态（hp<=0）保持原值不再扣血
                fresh = await self.db.get_player_by_id(player.user_id)
                cur_hp = fresh.hp if fresh else player.hp
                penalty_hp = int(cur_hp * 0.2) if cur_hp > 0 else 0
                new_hp = max(1, cur_hp - penalty_hp) if cur_hp > 0 else cur_hp
                if cur_hp > 0 and new_hp != cur_hp:
                    await self.db.conn.execute(
                        "UPDATE players SET hp = ? WHERE user_id = ?",
                        (new_hp, player.user_id),
                    )
                await self.db.conn.commit()
                player.gold = fresh.gold if fresh else player.gold - price
                player.hp = new_hp
            except Exception:
                await self.db.conn.rollback()
                raise
            await self._mark_black_market_bought(player.user_id, index)
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

        # 先条件原子扣灵石（防并发透支），发货失败再原路退回，绝不"扣钱又丢货"
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await self.db.conn.execute(
                "UPDATE players SET gold = gold - ? WHERE user_id = ? AND gold >= ?",
                (price, player.user_id, price),
            )
            if not cursor.rowcount:
                await self.db.conn.rollback()
                fresh = await self.db.get_player_by_id(player.user_id)
                have = fresh.gold if fresh else player.gold
                return False, (
                    f"❌ 灵石不足！需要 {price:,} 灵石，"
                    f"你当前只有 {have:,}。"
                )

            if is_pill:
                # 丹药在事务内基于最新数据直接入背包，避免旧快照覆盖并发入账
                fresh = await self.db.get_player_by_id(player.user_id)
                if fresh:
                    inventory = fresh.get_pills_inventory()
                    pill_key = f"{target['name']}|一般"
                    inventory[pill_key] = inventory.get(pill_key, 0) + 1
                    fresh.set_pills_inventory(inventory)
                    await self.db.update_player(fresh, commit=False)
                await self.db.conn.commit()
                player.gold = fresh.gold if fresh else player.gold - price
                if fresh:
                    player.set_pills_inventory(fresh.get_pills_inventory())
                await self._mark_black_market_bought(player.user_id, index)
                return True, (
                    f"✅ 交易成功！你获得了【{target['name']}】x1，已放入丹药背包。\n"
                    f"消耗灵石：{price:,}"
                )
            await self.db.conn.commit()
            player.gold -= price
        except Exception:
            await self.db.conn.rollback()
            raise

        # 非丹药走储物戒入库；store_item 自带事务与失败回滚，异常也不吞灵石
        try:
            ok, store_msg = await self.storage_ring_mgr.store_item(
                player, target["name"], 1, silent=True,
                quality=target.get("quality") or "",
            )
        except Exception as e:
            ok, store_msg = False, f"入库异常：{e}"
        if not ok:
            # 储物戒满/类型不可存：退回灵石，绝不发幽灵物品
            await self.db.add_player_gold(player.user_id, price)
            player.gold += price
            return False, (
                f"❌ 购买【{target['name']}】失败：{store_msg}\n"
                f"灵石已退回：{price:,}"
            )

        await self._mark_black_market_bought(player.user_id, index)
        return True, (
            f"✅ 交易成功！你获得了【{target['name']}】x1，已存入储物戒。\n"
            f"消耗灵石：{price:,}"
        )