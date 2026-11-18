# managers/treasure_map_manager.py
"""古修藏宝图爬塔：逐层深入、随时撤退的高风险探险。

核心约束（与需求一致）：
- 爬塔期间禁用自然恢复与调息恢复；
- 丹药/符箓回血有每层上限（默认最大气血的 18%），不能靠嗑药无限续命；
- 每层都可能触发陷阱/妖兽，气血归零则本次所有战利品尽失；
- 层数越深，掉落品阶与灵石越高，但受伤也更重。
"""

from __future__ import annotations

import json
import time
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger

from ..data import DataBase
from ..models import Player
from ..utils.game_secret import seeded_rng

__all__ = ["TreasureMapManager"]

MAX_FLOOR = 6
HEAL_BUDGET_RATE = 0.18
MIN_START_HP_RATE = 0.35
# 超过 24 小时没有推进的爬塔视为异常中断，由后台按撤退结算
STALE_RUN_SECONDS = 86400

MAP_TIERS = {
    1: {"name": "残破藏宝图", "price": 8000, "gold": 900, "loot_chance": 0.55, "desc": "边角残缺，只能看出前几层路径。"},
    2: {"name": "古修藏宝图", "price": 40000, "gold": 2600, "loot_chance": 0.68, "desc": "古修洞府遗图，灵纹尚存。"},
    3: {"name": "太古董藏图", "price": 180000, "gold": 7200, "loot_chance": 0.80, "desc": "以帝血绘制的太古图卷，深处封着大机缘。"},
}

FLOOR_RULES = {
    1: {"damage": (0.06, 0.12), "trap": 0.18, "trap_mult": 1.6, "label": "残破石阶"},
    2: {"damage": (0.09, 0.17), "trap": 0.26, "trap_mult": 1.7, "label": "腐骨回廊"},
    3: {"damage": (0.13, 0.22), "trap": 0.34, "trap_mult": 1.8, "label": "禁制甬道"},
    4: {"damage": (0.17, 0.28), "trap": 0.42, "trap_mult": 1.9, "label": "血池地宫"},
    5: {"damage": (0.22, 0.34), "trap": 0.50, "trap_mult": 2.0, "label": "万骨祭坛"},
    6: {"damage": (0.28, 0.42), "trap": 0.58, "trap_mult": 2.1, "label": "古修主墓"},
}


class TreasureMapManager:
    def __init__(self, db: DataBase, storage_ring_mgr, config_manager, combat_mgr=None, gamble_mgr=None):
        self.db = db
        self.storage_ring_mgr = storage_ring_mgr
        self.config_manager = config_manager
        self.combat_mgr = combat_mgr
        self.gamble_mgr = gamble_mgr

    # ================= 基础查询 =================
    async def _fetchone(self, sql: str, params: tuple = ()) -> Optional[dict]:
        async with self.db.conn.execute(sql, params) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        keys = [d[0] for d in cursor.description]
        return dict(zip(keys, row))

    async def _fetchall(self, sql: str, params: tuple = ()) -> List[dict]:
        async with self.db.conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            keys = [d[0] for d in cursor.description]
        return [dict(zip(keys, row)) for row in rows]

    @staticmethod
    async def is_climbing(db: DataBase, user_id: str) -> bool:
        try:
            async with db.conn.execute(
                "SELECT 1 FROM treasure_map_runs WHERE user_id = ? AND status = 'active'",
                (user_id,),
            ) as cursor:
                return await cursor.fetchone() is not None
        except Exception:
            return False

    async def get_active_run(self, user_id: str) -> Optional[dict]:
        run = await self._fetchone(
            "SELECT * FROM treasure_map_runs WHERE user_id = ? AND status = 'active'",
            (user_id,),
        )
        if not run:
            return None
        try:
            run["loot"] = json.loads(run.get("loot") or "[]")
        except Exception:
            run["loot"] = []
        try:
            run["log"] = json.loads(run.get("log") or "[]")
        except Exception:
            run["log"] = []
        return run

    async def _max_hp(self, player: Player) -> int:
        if self.combat_mgr:
            try:
                stats = self.combat_mgr.build_player_combat_stats(
                    player, self.config_manager, db=self.db
                )
                return max(1, int(stats.max_hp))
            except Exception:
                pass
        return max(1, int(player.max_blood_qi or 100))

    # ================= 回血上限 =================
    @staticmethod
    async def allow_heal(db: DataBase, user_id: str, requested: int, max_hp: int) -> int:
        """爬塔期间把一次回血裁剪到本层剩余额度；非爬塔状态原样返回。"""
        if requested <= 0:
            return requested
        try:
            async with db.conn.execute(
                "SELECT heal_used, heal_budget FROM treasure_map_runs "
                "WHERE user_id = ? AND status = 'active'",
                (user_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                return requested
            used, budget = int(row[0] or 0), int(row[1] or 0)
            remaining = max(0, budget - used)
            allowed = min(int(requested), remaining)
            if allowed > 0:
                await db.conn.execute(
                    "UPDATE treasure_map_runs SET heal_used = heal_used + ? "
                    "WHERE user_id = ? AND status = 'active'",
                    (allowed, user_id),
                )
                await db.conn.commit()
            return allowed
        except Exception:
            return requested

    # ================= 战斗写回裁剪 =================
    @staticmethod
    async def clamp_combat_hp(db: DataBase, user_id: str, new_hp: int) -> int:
        """战斗结束写回气血前裁剪爬塔玩家的治疗量。

        爬塔期间禁止自然恢复/调息，丹药回血也受每层额度限制。但切磋战后是
        「按满血写回」、决斗/世界Boss/悬赏的战斗内又有吸血与治疗，若直接落库
        就等于绕过额度。这里统一按 :meth:`allow_heal` 裁剪：掉血照常，
        回血只能吃掉本层剩余额度。非爬塔状态原样返回。
        """
        try:
            new_hp = int(new_hp)
        except (TypeError, ValueError):
            return new_hp
        try:
            async with db.conn.execute(
                "SELECT p.hp FROM treasure_map_runs r "
                "JOIN players p ON p.user_id = r.user_id "
                "WHERE r.user_id = ? AND r.status = 'active'",
                (user_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                return new_hp
            current = int(row[0] or 0)
            if new_hp <= current:
                return new_hp
            allowed = await TreasureMapManager.allow_heal(db, user_id, new_hp - current, 0)
            return current + max(0, int(allowed or 0))
        except Exception:
            return new_hp

    # ================= 战利品 =================
    def _roll_loot(self, tier: int, floor_no: int, rng) -> List[dict]:
        cfg = MAP_TIERS.get(tier, MAP_TIERS[1])
        loot: List[dict] = []
        if rng.random() > float(cfg["loot_chance"]) + floor_no * 0.03:
            return loot
        if self.gamble_mgr:
            try:
                stone_tier = min(5, tier + (1 if floor_no >= 4 else 0))
                hidden = self.gamble_mgr._roll_tier_loot(stone_tier, rng)
                if hidden.get("kind") != "dust":
                    loot.extend(hidden.get("loot") or [])
                if hidden.get("kind") == "material" and rng.random() < 0.35:
                    extra = self.gamble_mgr._roll_tier_loot(stone_tier, rng)
                    loot.extend(extra.get("loot") or [])
            except Exception as exc:
                logger.warning(f"【藏宝图】掉落生成失败: {exc}")
        if not loot:
            catalog = getattr(self.config_manager, "material_catalog", {}) or {}
            pool = [x for x in catalog.values() if isinstance(x, dict)]
            if pool:
                info = rng.choice(pool)
                loot.append({
                    "kind": "material", "name": info.get("name"),
                    "rank": info.get("tier") or "凡品", "qty": 1,
                })
        return loot

    def _loot_label(self, loot: dict) -> str:
        if self.gamble_mgr:
            try:
                return self.gamble_mgr._loot_label(loot)
            except Exception:
                pass
        name = loot.get("name") or "碎屑"
        qty = int(loot.get("qty") or 1)
        return f"【{name}】" + (f"x{qty}" if qty > 1 else "")

    # ================= 指令 =================
    async def get_status(self, player: Player) -> str:
        run = await self.get_active_run(player.user_id)
        if not run:
            return (
                "🗺️ 古修藏宝图\n"
                "━━━━━━━━━━━━━━━\n"
                "逐层深入古修墓府，每层都可能遇到陷阱与妖兽。\n"
                "气血归零则本次战利品尽失；可随时撤退落袋为安。\n"
                "⚠️ 爬塔期间无法自然恢复/调息；丹药回血每层有上限，符箓不能直接补战斗气血。\n"
                "⚠️ 战斗也不回血：切磋不按满血复原，决斗/世界Boss/悬赏的战斗吸血只吃本层剩余额度。\n"
                "━━━━━━━━━━━━━━━\n"
                "💡 `/藏宝图 开始 <1-3>` 购买并进入：\n"
                "  1 残破藏宝图 8,000 灵石\n"
                "  2 古修藏宝图 40,000 灵石\n"
                "  3 太古董藏图 180,000 灵石\n"
                "💡 `/藏宝图 继续` 深入一层｜`/藏宝图 撤退` 带走战利品"
            )
        max_hp = int(run.get("max_hp") or 1)
        hp = int(player.hp or 0)
        budget = int(run.get("heal_budget") or 0)
        used = int(run.get("heal_used") or 0)
        lines = [
            "🗺️ 古修藏宝图 · 爬塔中",
            "━━━━━━━━━━━━━━━",
            f"地图：{MAP_TIERS.get(int(run['map_tier']), {}).get('name', '未知')}",
            f"层数：第 {int(run['floor'])} / {MAX_FLOOR} 层",
            f"气血：{hp:,} / {max_hp:,}",
            f"本层回血额度：{max(0, budget - used):,} / {budget:,}",
            f"已获战利品：{len(run.get('loot') or [])} 件",
        ]
        for entry in (run.get("log") or [])[-4:]:
            lines.append(f"· {entry}")
        lines.extend([
            "━━━━━━━━━━━━━━━",
            "💡 `/藏宝图 继续` 深入｜`/藏宝图 撤退` 结算带走",
            "⚠️ 越深掉落越好，但陷阱伤害与触发率也越高。",
            "⚠️ 战斗回血同样只吃本层额度；超过 24 小时未推进会由后台按撤退结算。",
        ])
        return "\n".join(lines)

    async def start(self, player: Player, tier: int = 1) -> Tuple[bool, str]:
        tier = int(tier or 1)
        cfg = MAP_TIERS.get(tier)
        if not cfg:
            return False, "❌ 地图档位只能是 1 / 2 / 3。"
        if await self.get_active_run(player.user_id):
            return False, "❌ 你已经在爬塔途中，先 /藏宝图 继续 或 /藏宝图 撤退。"
        busy = None
        try:
            busy = await self.db.ext.first_busy_cd(player.user_id)
        except Exception:
            busy = None
        if busy is not None:
            from ..models_extended import UserStatus

            name = UserStatus.get_name(int(busy.type))
            return False, f"❌ 你正在{name}，无法同时进入藏宝图，先结束当前活动。"
        max_hp = await self._max_hp(player)
        hp = int(player.hp or 0)
        if hp <= 0:
            return False, "❌ 你当前气血不足，先恢复后再进藏宝图。"
        if hp < max_hp * MIN_START_HP_RATE:
            return False, (
                f"❌ 气血低于 {int(MIN_START_HP_RATE * 100)}% 时进入必死无疑。\n"
                f"当前气血：{hp:,}/{max_hp:,}，请先调息恢复。"
            )
        price = int(cfg["price"])
        if player.gold < price:
            return False, f"❌ 购买【{cfg['name']}】需要 {price:,} 灵石，你当前只有 {player.gold:,}。"

        now = int(time.time())
        budget = max(1, int(max_hp * HEAL_BUDGET_RATE))
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await self.db.conn.execute(
                "UPDATE players SET gold = gold - ? WHERE user_id = ? AND gold >= ?",
                (price, player.user_id, price),
            )
            if not cursor.rowcount:
                await self.db.conn.rollback()
                return False, "❌ 灵石不足，购买失败。"
            await self.db.conn.execute(
                "INSERT INTO treasure_map_runs "
                "(user_id, floor, map_tier, hp, max_hp, heal_used, heal_budget, loot, log, status, started_at, updated_at) "
                "VALUES (?, 0, ?, ?, ?, 0, ?, '[]', '[]', 'active', ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET floor=0, map_tier=excluded.map_tier, "
                "hp=excluded.hp, max_hp=excluded.max_hp, heal_used=0, heal_budget=excluded.heal_budget, "
                "loot='[]', log='[]', status='active', started_at=excluded.started_at, updated_at=excluded.updated_at",
                (player.user_id, tier, hp, max_hp, budget, now, now),
            )
            await self.db.conn.commit()
            player.gold -= price
        except Exception:
            await self.db.conn.rollback()
            raise
        return True, (
            f"🗺️ 你花费 {price:,} 灵石买下【{cfg['name']}】，踏入古修墓府。\n"
            f"━━━━━━━━━━━━━━━\n"
            f"{cfg['desc']}\n"
            f"当前气血：{hp:,}/{max_hp:,}\n"
            f"⚠️ 爬塔期间无法自然恢复/调息；丹药回血每层最多 {budget:,}，符箓不能直接补战斗气血。\n"
            f"⚠️ 战斗回血（切磋满血复原、决斗/世界Boss/悬赏吸血）同样只吃这 {budget:,} 的额度。\n"
            f"💡 `/藏宝图 继续` 深入第一层"
        )

    async def advance(self, player: Player) -> Tuple[bool, str]:
        run = await self.get_active_run(player.user_id)
        if not run:
            return False, "❌ 你当前没有在爬塔。使用 `/藏宝图 开始 <1-3>` 进入。"
        floor_no = int(run["floor"]) + 1
        if floor_no > MAX_FLOOR:
            return await self._settle(player, run, success=True, reason="已通关全图")
        rule = FLOOR_RULES.get(floor_no, FLOOR_RULES[MAX_FLOOR])
        max_hp = int(run.get("max_hp") or 1)
        rng = seeded_rng("map", player.user_id, run["started_at"], floor_no)

        # 先结算本层伤害
        low, high = rule["damage"]
        damage_pct = rng.uniform(low, high)
        trap = rng.random() < rule["trap"]
        if trap:
            damage_pct *= rule["trap_mult"]
        damage = max(1, int(max_hp * damage_pct))
        hp_before = int(player.hp or 0)
        hp_after = hp_before - damage

        log_entries = list(run.get("log") or [])
        if trap:
            log_entries.append(f"第{floor_no}层触发陷阱，气血 -{damage:,}")
        else:
            log_entries.append(f"第{floor_no}层环境侵蚀，气血 -{damage:,}")

        if hp_after <= 0:
            # 失败：清空战利品，气血压到 1，结束爬塔
            old_loot = list(run.get("loot") or [])
            lost_items = [x for x in old_loot if x.get("kind") != "gold"]
            lost_gold = sum(int(x.get("amount") or 0) for x in old_loot if x.get("kind") == "gold")
            await self.db.conn.execute(
                "UPDATE players SET hp = 1 WHERE user_id = ?", (player.user_id,)
            )
            await self.db.conn.execute(
                "UPDATE treasure_map_runs SET status='ended', floor=?, log=?, updated_at=? "
                "WHERE user_id = ?",
                (floor_no, json.dumps(log_entries[-12:], ensure_ascii=False), int(time.time()), player.user_id),
            )
            await self.db.conn.commit()
            player.hp = 1
            lost_desc = f"本次获得的 {len(lost_items)} 件战利品"
            if lost_gold:
                lost_desc += f"与 {lost_gold:,} 灵石"
            return True, (
                f"💀 你在第 {floor_no} 层倒下了！\n"
                f"━━━━━━━━━━━━━━━\n"
                f"气血被打空，{lost_desc}全部遗失。\n"
                f"你拖着残躯爬出墓府，气血仅余 1 点。\n"
                f"⚠️ 下次请在气血见底前及时撤退。"
            )

        # 再结算本层收获
        gold_gain = 0
        loot = list(run.get("loot") or [])
        cfg = MAP_TIERS.get(int(run["map_tier"]), MAP_TIERS[1])
        if rng.random() < 0.78:
            gold_gain = int(cfg["gold"] * floor_no * rng.uniform(0.7, 1.3))
            # 层内灵石先记入战利品，撤退/通关时统一结算；中途倒下则随战利品一起丢失
            loot.append({"kind": "gold", "amount": gold_gain})
            log_entries.append(f"拾得灵石 {gold_gain:,}（撤退时结算）")
        new_loot = self._roll_loot(int(run["map_tier"]), floor_no, rng)
        for item in new_loot:
            loot.append(item)
            log_entries.append("发现 " + self._loot_label(item))
        if not gold_gain and not new_loot:
            log_entries.append("这一层空空如也，只有尘土")

        if floor_no >= MAX_FLOOR:
            # 最后一层：自动结算并给通关额外奖励。
            # 先把本层的 floor/loot/log 落库再结算，保证数据库里的记录
            # 与实际发放的战利品一致（否则 _settle 回滚后无法排查发了什么）。
            run["floor"] = floor_no
            run["loot"] = loot
            run["log"] = log_entries
            player.hp = hp_after
            await self.db.conn.execute(
                "UPDATE players SET hp = ? WHERE user_id = ?", (hp_after, player.user_id)
            )
            await self.db.conn.execute(
                "UPDATE treasure_map_runs SET floor=?, hp=?, loot=?, log=?, updated_at=? "
                "WHERE user_id = ? AND status='active'",
                (
                    floor_no, hp_after,
                    json.dumps(loot, ensure_ascii=False),
                    json.dumps(log_entries[-12:], ensure_ascii=False),
                    int(time.time()), player.user_id,
                ),
            )
            await self.db.conn.commit()
            return await self._settle(player, run, success=True, reason="通关主墓")

        budget = max(1, int(max_hp * HEAL_BUDGET_RATE))
        await self.db.conn.execute(
            "UPDATE players SET hp = ? WHERE user_id = ?", (hp_after, player.user_id)
        )
        await self.db.conn.execute(
            "UPDATE treasure_map_runs SET floor=?, hp=?, heal_used=0, heal_budget=?, loot=?, log=?, updated_at=? "
            "WHERE user_id = ?",
            (
                floor_no, hp_after, budget,
                json.dumps(loot, ensure_ascii=False),
                json.dumps(log_entries[-12:], ensure_ascii=False),
                int(time.time()), player.user_id,
            ),
        )
        await self.db.conn.commit()
        player.hp = hp_after
        return True, (
            f"🗺️ 第 {floor_no} 层 · {rule['label']}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"气血 -{damage:,} → {hp_after:,}/{max_hp:,}"
            f"{'（陷阱！）' if trap else ''}\n"
            f"灵石 +{gold_gain:,}（撤退/通关时结算）\n"
            f"战利品：{sum(1 for x in loot if x.get('kind') != 'gold')} 件\n"
            f"━━━━━━━━━━━━━━━\n"
            + ("\n".join(log_entries[-3:]) + "\n")
            + f"💡 /藏宝图 继续 深入第 {floor_no + 1} 层｜/藏宝图 撤退 落袋为安\n"
            f"⚠️ 下一层伤害更高，撤退保留现有全部战利品。"
        )

    async def settle_stale_runs(self, now: Optional[int] = None,
                                stale_seconds: int = STALE_RUN_SECONDS) -> int:
        """后台清理长时间没有推进的爬塔。

        旧实现直接把 run 置为 ended，玩家攒在 ``loot`` 里的灵石与战利品会被
        静默吞掉（既没发放也没提示）。这里改为按「撤退」结算：
        已经活着拿到的东西照常带走，只是不能再继续深入。
        """
        now = int(now or time.time())
        rows = await self._fetchall(
            "SELECT user_id FROM treasure_map_runs "
            "WHERE status = 'active' AND updated_at + ? < ?",
            (int(stale_seconds), now),
        )
        settled = 0
        for row in rows:
            user_id = str(row.get("user_id") or "")
            if not user_id:
                continue
            try:
                player = await self.db.get_player_by_id(user_id)
                run = await self.get_active_run(user_id)
                if not player or not run:
                    # 角色已不存在：直接结束，避免每 60 秒重复扫描同一条僵尸记录
                    await self.db.conn.execute(
                        "UPDATE treasure_map_runs SET status='ended', updated_at=? "
                        "WHERE user_id = ? AND status='active'",
                        (now, user_id),
                    )
                    await self.db.conn.commit()
                    continue
                ok, msg = await self._settle(
                    player, run, success=True, reason="墓府封闭（超时自动结算）"
                )
                if ok:
                    settled += 1
                    logger.info(f"【藏宝图】超时结算 {user_id}：{msg.splitlines()[2] if len(msg.splitlines()) > 2 else ''}")
            except Exception as exc:
                logger.warning(f"【藏宝图】超时结算失败 {user_id}: {exc}")
        return settled

    async def retreat(self, player: Player) -> Tuple[bool, str]:
        run = await self.get_active_run(player.user_id)
        if not run:
            return False, "❌ 你当前没有在爬塔。"
        return await self._settle(player, run, success=True, reason="主动撤退")

    async def _settle(self, player: Player, run: dict, success: bool,
                      reason: str, extra_gold: int = 0) -> Tuple[bool, str]:
        all_loot = list(run.get("loot") or [])
        loot = [item for item in all_loot if item.get("kind") != "gold"]
        loot_gold = sum(
            int(item.get("amount") or 0) for item in all_loot if item.get("kind") == "gold"
        )
        labels = []
        gold_bonus = 0
        if success:
            gold_bonus = int(extra_gold or 0) + loot_gold
            if reason == "通关主墓":
                cfg = MAP_TIERS.get(int(run.get("map_tier")), MAP_TIERS[1])
                gold_bonus += int(cfg["price"] * 1.8)
            elif reason == "已通关全图":
                cfg = MAP_TIERS.get(int(run.get("map_tier")), MAP_TIERS[1])
                gold_bonus += int(cfg["price"] * 1.5)
        # 「结束本次爬塔」与发奖必须同事务并带 status 守卫：
        # 否则中途异常会留下 active 的 run，玩家再撤退一次就能领双份。
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await self.db.conn.execute(
                "UPDATE treasure_map_runs SET status='ended', updated_at=? "
                "WHERE user_id = ? AND status='active'",
                (int(time.time()), player.user_id),
            )
            if not cursor.rowcount:
                await self.db.conn.rollback()
                return False, "❌ 本次爬塔已经结算过了。"
            if success:
                if self.gamble_mgr:
                    for item in loot:
                        try:
                            if item.get("kind") == "jewel" and isinstance(item.get("jewel"), dict):
                                gem = self.gamble_mgr.jewel_manager.add_jewel(player, dict(item["jewel"]))
                                labels.append(f"💎【{gem.get('name')}·{gem.get('quality')}】")
                            else:
                                labels.append(await self.gamble_mgr._award_loot(player, item, item.get("quality") or ""))
                        except Exception as exc:
                            logger.warning(f"【藏宝图】发放战利品失败: {exc}")
                else:
                    for item in loot:
                        name = item.get("name")
                        if not name:
                            continue
                        ok, _msg = await self.storage_ring_mgr.store_item(
                            player, name, int(item.get("qty") or 1), silent=True,
                            external_transaction=True,
                        )
                        if ok:
                            labels.append(f"【{name}】")
                # 事务内重读灵石：后台暗标退款/盘口派彩不持玩家锁，
                # 直接用命令开始时的旧快照回写会把它们的入账覆盖掉。
                fresh = await self._fetchone(
                    "SELECT gold FROM players WHERE user_id = ?", (player.user_id,)
                )
                base_gold = int((fresh or {}).get("gold") or 0) if fresh else int(player.gold or 0)
                player.gold = base_gold + gold_bonus
            await self.db.update_player(player, commit=False)
            await self.db.conn.commit()
        except Exception:
            await self.db.conn.rollback()
            raise
        lines = [
            f"🗺️ 藏宝图结算 · {reason}",
            "━━━━━━━━━━━━━━━",
            f"到达层数：第 {int(run.get('floor') or 0)} 层",
            f"灵石收获：+{gold_bonus:,}",
        ]
        if labels:
            lines.append("物品收获：" + "、".join(labels[:8]) + ("…" if len(labels) > 8 else ""))
        elif loot:
            lines.append("物品收获：无（储物戒空间不足或发放失败）")
        elif loot_gold:
            lines.append("物品收获：无（只带回了灵石）")
        else:
            lines.append("物品收获：空手而归")
        lines.extend([
            "━━━━━━━━━━━━━━━",
            f"💰 当前灵石：{player.gold:,}",
            "💡 可再次 `/藏宝图 开始 <1-3>` 重新探险。",
        ])
        return True, "\n".join(lines)