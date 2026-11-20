# managers/mine_vein_manager.py
"""矿脉勘探权：限时产矿、随机矿难事件与合伙人分红。

玩法要点：
- 每小时刷新 5 档矿脉，购买后获得限时开采权；
- 产量由隐藏「富集度」决定，列表只给模糊灵压提示；
- 每次开采按小时结算，并随机触发塌方、妖兽、富矿等事件；
- 矿脉到期时，后台会自动把剩余产量结算给矿主与合伙人；
- 矿主可邀请合伙人（需对方接受），双方按份额各自领取。
"""

from __future__ import annotations

import json
import time
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger

from ..data import DataBase
from ..models import Player
from ..utils.game_secret import seeded_rng

__all__ = ["MineVeinManager"]

MINE_TEMPLATES = (
    {"name": "贫瘠灵脉", "price": 12000, "yield": 1400, "hours": 12, "risk": "低·试水", "tier": 1},
    {"name": "小型矿脉", "price": 45000, "yield": 5200, "hours": 18, "risk": "中", "tier": 2},
    {"name": "富集矿脉", "price": 150000, "yield": 18500, "hours": 24, "risk": "高", "tier": 3},
    {"name": "玄铁矿脉", "price": 420000, "yield": 56000, "hours": 24, "risk": "极高", "tier": 4},
    {"name": "太古灵矿", "price": 1200000, "yield": 165000, "hours": 24, "risk": "九死一生", "tier": 5},
)
MAX_ACTIVE_MINES = 2
MAX_PARTNER_TOTAL = 80

EVENTS = (
    ("normal", 52, 1.0, "平稳出矿"),
    ("rich", 16, 1.9, "挖到富矿脉，产量翻倍"),
    ("spirit", 5, 2.6, "灵气喷涌，伴生灵材"),
    ("collapse", 15, 0.45, "矿道塌方，产量锐减"),
    ("beast", 12, 0.55, "妖兽袭扰矿工，产量受损"),
)


class MineVeinManager:
    def __init__(self, db: DataBase, storage_ring_mgr, config_manager, notifier=None):
        self.db = db
        self.storage_ring_mgr = storage_ring_mgr
        self.config_manager = config_manager
        # 与自动代理共用同一条消息链路（main._send_to_origin）：
        # 网页端落个人消息历史，QQ 端直接发送到购买/接受邀请时的会话。
        self.notifier = notifier

    async def _notify(self, origin: str, msg: str, user_id: str = "", title: str = "矿脉结算"):
        if not origin or not self.notifier or not msg:
            return
        try:
            # 新回调支持 title；测试/外部实现的精简回调仍按两参或三参调用
            await self.notifier(origin, msg, user_id, title=title)
        except TypeError:
            try:
                await self.notifier(origin, msg, user_id)
            except TypeError:
                try:
                    await self.notifier(origin, msg)
                except Exception as e:
                    logger.warning(f"【矿脉】结算消息发送失败: {e}")
            except Exception as e:
                logger.warning(f"【矿脉】结算消息发送失败: {e}")
        except Exception as e:
            logger.warning(f"【矿脉】结算消息发送失败: {e}")

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
    def _hour_key(ts: Optional[int] = None) -> int:
        return int(ts or time.time()) // 3600

    def _candidates(self, user_id: str) -> List[dict]:
        hour = self._hour_key()
        result = []
        for idx, tpl in enumerate(MINE_TEMPLATES, 1):
            rng = seeded_rng("mine", hour, user_id, idx)
            price = max(100, int(tpl["price"] * rng.uniform(0.92, 1.08)))
            richness = int(rng.uniform(62, 142))
            if richness >= 120:
                hint, hint_true = "灵压炽盛，似有大矿", True
            elif richness >= 95:
                hint, hint_true = "灵压平稳，成色尚可", True
            elif richness >= 78:
                hint, hint_true = "灵压偏弱，聊胜于无", False
            else:
                hint, hint_true = "矿脉枯竭，慎入", False
            # 20% 概率给假提示
            if rng.random() < 0.20:
                hint = rng.choice([
                    "灵压炽盛，似有大矿", "灵压平稳，成色尚可",
                    "灵压偏弱，聊胜于无", "矿脉枯竭，慎入",
                ])
                hint_true = None
            result.append({
                "index": idx, "name": tpl["name"], "price": price,
                "hours": tpl["hours"], "risk": tpl["risk"], "tier": tpl["tier"],
                "richness": richness, "hint": hint, "hint_true": hint_true,
                "base_yield": tpl["yield"], "hour": hour,
            })
        return result

    def build_mine_entries(self, user_id: str) -> list:
        entries = []
        for item in self._candidates(user_id):
            entries.append({
                "id": item["index"],
                "name": item["name"],
                "rank": f"矿脉·{item['tier']}阶",
                "kind": "勘探权",
                "price": item["price"],
                "currency": "灵石",
                "effect": item["hint"],
                "effectLabel": "灵压勘探",
                "state": "ok",
                "cmd": f"/矿脉 购买 {item['index']}",
                "source": "mine",
                "action": "购买",
                "notes": [
                    f"限时 {item['hours']} 小时",
                    f"风险：{item['risk']}",
                    "每小时可结算一次产量",
                ],
            })
        return entries

    async def get_market(self, user_id: str) -> str:
        lines = ["⛏️ 矿脉勘探权（每小时刷新）", "━━━━━━━━━━━━━━━"]
        for item in self._candidates(user_id):
            lines.append(
                f"[{item['index']}] {item['name']} - {item['price']:,} 灵石"
                f"（限时 {item['hours']} 小时｜风险：{item['risk']}）"
            )
            lines.append(f"    🔎 勘探提示：{item['hint']}")
        lines.extend([
            "━━━━━━━━━━━━━━━",
            "💡 `/矿脉 购买 <编号>` 买下勘探权｜`/矿脉 我的` 查看自有矿脉",
            "💡 `/矿脉 开采 <编号>` 按小时结算产量（可随时结算）",
            "💡 到期后剩余产量会自动结算入账，无需手动领取",
            "🤝 `/矿脉 合伙 <编号> <道号> <份额%>` 邀请合伙人（对方需接受）",
            "⚠️ 矿脉会触发塌方/妖兽等事件，收益随事件波动。",
        ])
        return "\n".join(lines)

    async def my_mines(self, user_id: str) -> str:
        mines = await self._fetchall(
            "SELECT * FROM mine_veins WHERE owner_id = ? AND status = 'active' ORDER BY id DESC",
            (user_id,),
        )
        partner_rows = await self._fetchall(
            "SELECT mine_id, share FROM mine_partners "
            "WHERE user_id = ? AND status = 'accepted' ORDER BY mine_id DESC",
            (user_id,),
        )
        joined: List[dict] = []
        joined_share: Dict[int, int] = {}
        if partner_rows:
            joined_share = {int(r["mine_id"]): int(r.get("share") or 0) for r in partner_rows}
            ids = list(joined_share)
            placeholders = ",".join("?" for _ in ids)
            joined = await self._fetchall(
                f"SELECT * FROM mine_veins WHERE id IN ({placeholders}) AND status = 'active' "
                "ORDER BY id DESC",
                tuple(ids),
            )
        pending = await self._fetchall(
            "SELECT mp.*, mv.name AS mine_name, mv.tier AS mine_tier, "
            "mv.expire_at AS mine_expire_at, mv.owner_id AS mine_owner "
            "FROM mine_partners mp JOIN mine_veins mv ON mv.id = mp.mine_id "
            "WHERE mp.user_id = ? AND mp.status = 'pending' AND mv.status = 'active' "
            "ORDER BY mp.mine_id DESC",
            (user_id,),
        )
        if not mines and not joined and not pending:
            return "⛏️ 你当前没有矿脉。使用 `/矿脉` 查看可购买的勘探权。"

        lines: List[str] = []
        if mines:
            lines.extend(["⛏️ 我的矿脉", "━━━━━━━━━━━━━━━"])
            now = int(time.time())
            for mine in mines:
                partners = await self._fetchall(
                    "SELECT * FROM mine_partners WHERE mine_id = ? AND status = 'accepted'",
                    (mine["id"],),
                )
                shares = sum(int(p.get("share") or 0) for p in partners)
                remaining = max(0, int(mine["expire_at"]) - now)
                lines.append(
                    f"[{mine['id']}] {mine['name']}（{mine['tier']}阶）"
                    f" 剩余 {remaining // 3600} 时 {(remaining % 3600) // 60} 分"
                )
                lines.append(
                    f"    基础时产：{int(mine['base_yield']):,} 灵石｜你的份额：{100 - shares}%"
                )
                if partners:
                    names = []
                    for p in partners:
                        player = await self.db.get_player_by_id(p["user_id"])
                        names.append(f"{(player.user_name if player else p['user_id'])} {p['share']}%")
                    lines.append("    合伙人：" + "、".join(names))
                waiting = await self._fetchall(
                    "SELECT * FROM mine_partners WHERE mine_id = ? AND status = 'pending'",
                    (mine["id"],),
                )
                if waiting:
                    lines.append(f"    ⏳ 待接受邀请：{len(waiting)} 人")
        if joined:
            if lines:
                lines.append("")
            lines.extend(["🤝 我参与的矿脉", "━━━━━━━━━━━━━━━"])
            now = int(time.time())
            for mine in joined:
                share = joined_share.get(int(mine["id"]), 0)
                remaining = max(0, int(mine["expire_at"]) - now)
                owner = await self.db.get_player_by_id(mine["owner_id"])
                owner_name = owner.user_name if owner else mine["owner_id"]
                lines.append(
                    f"[{mine['id']}] {mine['name']}（{mine['tier']}阶）"
                    f" 矿主：{owner_name}"
                    f" 剩余 {remaining // 3600} 时 {(remaining % 3600) // 60} 分"
                )
                lines.append(
                    f"    基础时产：{int(mine['base_yield']):,} 灵石｜你的份额：{share}%"
                )
        if pending:
            if lines:
                lines.append("")
            lines.extend(["📨 待处理的合伙邀请", "━━━━━━━━━━━━━━━"])
            for row in pending:
                owner = await self.db.get_player_by_id(row["mine_owner"])
                owner_name = owner.user_name if owner else row["mine_owner"]
                lines.append(
                    f"[{row['mine_id']}] {row['mine_name']}（{row['mine_tier']}阶）"
                    f" 矿主：{owner_name}｜邀请份额：{int(row['share'])}%"
                )
            lines.append("💡 `/矿脉 接受 <编号>` 接受｜`/矿脉 拒绝 <编号>` 拒绝")
        lines.extend([
            "━━━━━━━━━━━━━━━",
            "💡 `/矿脉 开采 <编号>` 结算你的份额",
            "💡 到期时剩余产量会自动结算入账",
        ])
        return "\n".join(lines)

    async def buy(self, player: Player, index: int, origin: str = "") -> Tuple[bool, str]:
        candidates = self._candidates(player.user_id)
        if index < 1 or index > len(candidates):
            return False, "❌ 无效的矿脉编号，请使用 `/矿脉` 查看。"
        item = candidates[index - 1]
        active = await self._fetchone(
            "SELECT COUNT(*) AS n FROM mine_veins WHERE owner_id = ? AND status = 'active'",
            (player.user_id,),
        )
        if active and int(active.get("n") or 0) >= MAX_ACTIVE_MINES:
            return False, f"❌ 你最多同时持有 {MAX_ACTIVE_MINES} 条矿脉，先等旧的到期。"
        price = int(item["price"])
        if player.gold < price:
            return False, f"❌ 购买【{item['name']}】需要 {price:,} 灵石。"
        now = int(time.time())
        expire = now + int(item["hours"]) * 3600
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            # 持有上限必须在事务内复核：只在事务外读一次的话，
            # 两个并发请求都能通过校验，玩家就同时持有 3 条以上矿脉。
            cursor = await self.db.conn.execute(
                "SELECT COUNT(*) FROM mine_veins WHERE owner_id = ? AND status = 'active'",
                (player.user_id,),
            )
            row = await cursor.fetchone()
            if int((row[0] if row else 0) or 0) >= MAX_ACTIVE_MINES:
                await self.db.conn.rollback()
                return False, f"❌ 你最多同时持有 {MAX_ACTIVE_MINES} 条矿脉，先等旧的到期。"
            cursor = await self.db.conn.execute(
                "UPDATE players SET gold = gold - ? WHERE user_id = ? AND gold >= ?",
                (price, player.user_id, price),
            )
            if not cursor.rowcount:
                await self.db.conn.rollback()
                return False, "❌ 灵石不足，购买失败。"
            cursor = await self.db.conn.execute(
                "INSERT INTO mine_veins "
                "(owner_id, vein_key, name, tier, buy_price, base_yield, richness, "
                "created_at, expire_at, owner_claimed_hour, origin, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')",
                (
                    player.user_id, f"{item['hour']}:{index}", item["name"], item["tier"],
                    price, item["base_yield"], item["richness"], now, expire,
                    self._hour_key(now), str(origin or ""),
                ),
            )
            mine_id = cursor.lastrowid
            await self.db.conn.commit()
            player.gold -= price
        except Exception:
            await self.db.conn.rollback()
            raise
        return True, (
            f"⛏️ 你买下【{item['name']}】勘探权（编号 {mine_id}）。\n"
            f"━━━━━━━━━━━━━━━\n"
            f"勘探提示：{item['hint']}\n"
            f"期限：{item['hours']} 小时｜基础时产：{item['base_yield']:,} 灵石\n"
            f"💡 `/矿脉 开采 {mine_id}` 结算产量，`/矿脉 合伙 {mine_id} <道号> <份额%>` 招合伙人\n"
            f"✅ 到期时剩余产量会自动结算入账，不用担心忘记领取。"
        )

    async def invite_partner(self, player: Player, mine_id: int, target: str, share: int) -> Tuple[bool, str]:
        mine = await self._fetchone("SELECT * FROM mine_veins WHERE id = ? AND owner_id = ?", (mine_id, player.user_id))
        if not mine:
            return False, "❌ 这不是你的矿脉。"
        if int(time.time()) >= int(mine["expire_at"]):
            return False, "❌ 矿脉已经到期。"
        share = max(5, min(40, int(share)))
        target = str(target or "").strip()
        if not target:
            return False, "❌ 请填写合伙人的道号或 ID。"
        partner = None
        try:
            partner = await self.db.get_player_by_id(target)
        except Exception:
            partner = None
        if not partner:
            async with self.db.conn.execute(
                "SELECT * FROM players WHERE user_name = ? LIMIT 1", (target,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    keys = [d[0] for d in cursor.description]
                    data = dict(zip(keys, row))
                    partner = await self.db.get_player_by_id(data["user_id"])
        if not partner:
            return False, f"❌ 找不到道号为【{target}】的玩家。"
        if partner.user_id == player.user_id:
            return False, "❌ 不能邀请自己当合伙人。"
        accepted = await self._fetchall(
            "SELECT * FROM mine_partners WHERE mine_id = ? AND status = 'accepted'", (mine_id,)
        )
        used = sum(int(p.get("share") or 0) for p in accepted)
        if used + share > MAX_PARTNER_TOTAL:
            return False, f"❌ 合伙人份额已达上限（合计最多 {MAX_PARTNER_TOTAL}%）。"
        existing = await self._fetchone(
            "SELECT * FROM mine_partners WHERE mine_id = ? AND user_id = ?", (mine_id, partner.user_id)
        )
        if existing and existing["status"] == "accepted":
            return False, "❌ 对方已经是你的合伙人。"
        await self.db.conn.execute(
            "INSERT INTO mine_partners (mine_id, user_id, share, invested, status, last_claim_hour, created_at) "
            "VALUES (?, ?, ?, 0, 'pending', ?, ?) "
            "ON CONFLICT(mine_id, user_id) DO UPDATE SET share = excluded.share, "
            "status = 'pending', created_at = excluded.created_at",
            (mine_id, partner.user_id, share, self._hour_key(), int(time.time())),
        )
        await self.db.conn.commit()
        return True, (
            f"🤝 已向【{partner.user_name or partner.user_id}】发出合伙邀请：矿脉 {mine_id}，份额 {share}%。\n"
            f"💡 对方可发送 `/矿脉 接受 {mine_id}` 或 `/矿脉 拒绝 {mine_id}`。"
        )

    async def respond_invite(
        self, player: Player, mine_id: int, accept: bool, origin: str = ""
    ) -> Tuple[bool, str]:
        row = await self._fetchone(
            "SELECT * FROM mine_partners WHERE mine_id = ? AND user_id = ? AND status = 'pending'",
            (mine_id, player.user_id),
        )
        if not row:
            return False, "❌ 没有找到待处理的合伙邀请。"
        mine = await self._fetchone("SELECT * FROM mine_veins WHERE id = ?", (mine_id,))
        if not mine or int(mine["expire_at"]) <= int(time.time()):
            return False, "❌ 该矿脉已到期，邀请失效。"
        if not accept:
            await self.db.conn.execute(
                "UPDATE mine_partners SET status = 'rejected' WHERE mine_id = ? AND user_id = ? "
                "AND status = 'pending'",
                (mine_id, player.user_id),
            )
            await self.db.conn.commit()
            return True, "✅ 已拒绝该合伙邀请。"
        # 接受前复核份额：邀请发出后矿主可能又招募了其他合伙人
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            pending = await self._fetchone(
                "SELECT * FROM mine_partners WHERE mine_id = ? AND user_id = ? AND status = 'pending'",
                (mine_id, player.user_id),
            )
            if not pending:
                await self.db.conn.rollback()
                return False, "❌ 该邀请已失效。"
            accepted = await self._fetchall(
                "SELECT * FROM mine_partners WHERE mine_id = ? AND status = 'accepted' "
                "AND user_id != ?",
                (mine_id, player.user_id),
            )
            used = sum(int(p.get("share") or 0) for p in accepted)
            share = int(pending.get("share") or 0)
            if used + share > MAX_PARTNER_TOTAL:
                await self.db.conn.rollback()
                return False, f"❌ 合伙人份额已满（合计最多 {MAX_PARTNER_TOTAL}%），无法接受。"
            cursor = await self.db.conn.execute(
                "UPDATE mine_partners SET status = 'accepted', last_claim_hour = ?, origin = ? "
                "WHERE mine_id = ? AND user_id = ? AND status = 'pending'",
                (self._hour_key(), str(origin or ""), mine_id, player.user_id),
            )
            if not cursor.rowcount:
                await self.db.conn.rollback()
                return False, "❌ 该邀请已失效。"
            await self.db.conn.commit()
        except Exception:
            await self.db.conn.rollback()
            raise
        row = {"share": share}
        return True, (
            f"✅ 你已成为矿脉 {mine_id} 的合伙人，份额 {row['share']}%。\n"
            f"💡 使用 `/矿脉 开采 {mine_id}` 领取你的分成。"
        )

    async def _roll_event(self, mine_id: int, hour: int) -> Tuple[str, float, str]:
        rng = seeded_rng("mine-event", mine_id, hour)
        total = sum(weight for _, weight, _, _ in EVENTS)
        roll = rng.uniform(0, total)
        acc = 0
        for key, weight, mult, text in EVENTS:
            acc += weight
            if roll <= acc:
                return key, mult, text
        return "normal", 1.0, "平稳出矿"

    def _material_bonus(self, mine: dict, hour: int, rng) -> Optional[dict]:
        catalog = getattr(self.config_manager, "material_catalog", {}) or {}
        pool = [
            v for v in catalog.values()
            if isinstance(v, dict) and v.get("category") in ("矿材", "灵植")
            and str(v.get("tier") or "") != "圣品"
        ]
        if not pool:
            return None
        return rng.choice(pool)

    async def _settle_claim_for(
        self,
        mine: dict,
        player: Player,
        is_owner: bool,
        partner: Optional[dict],
        now: int,
    ) -> Tuple[bool, str, dict]:
        """计算并发放某玩家在一条矿脉上的剩余产量。

        矿主与合伙人的结算逻辑完全一致，因此手动开采与到期自动结算共用这里，
        避免两条路径算出不同的产量。并发安全依赖 ``last_*_hour`` 的条件更新。
        """
        mine_id = int(mine["id"])
        if is_owner:
            shares = await self._fetchall(
                "SELECT * FROM mine_partners WHERE mine_id = ? AND status = 'accepted'", (mine_id,)
            )
            used = sum(int(p.get("share") or 0) for p in shares)
            share = max(0, 100 - used)
            last_hour = int(mine["owner_claimed_hour"] or 0)
        else:
            share = int((partner or {}).get("share") or 0)
            last_hour = int((partner or {}).get("last_claim_hour") or 0)

        start_hour = max(int(mine["created_at"]) // 3600, last_hour)
        end_hour = min(self._hour_key(now), int(mine["expire_at"]) // 3600)
        hours = max(0, end_hour - start_hour)
        if hours <= 0:
            return False, "⏳ 还没有新的整点产量，过一段时间再来开采。", {}
        if hours > 48:
            # 超出上限的部分留到下次结算，不能让领取进度直接跳过它们
            hours = 48
            end_hour = start_hour + hours

        total_gold = 0
        material_total: Dict[str, int] = {}
        event_lines = []
        for h in range(start_hour + 1, start_hour + 1 + hours):
            key, mult, text = await self._roll_event(mine_id, h)
            rng = seeded_rng("mine-gain", mine_id, h, player.user_id)
            per_hour = int(int(mine["base_yield"]) * (int(mine["richness"]) / 100.0) * mult * (share / 100.0))
            per_hour = int(per_hour * rng.uniform(0.92, 1.08))
            total_gold += max(0, per_hour)
            if key in ("rich", "spirit") and share > 0:
                info = self._material_bonus(mine, h, rng)
                if info:
                    n = 1 if key == "rich" else 2
                    material_total[info["name"]] = material_total.get(info["name"], 0) + n
            if key not in ("normal",):
                event_lines.append(f"第{h % 24:02d}时：{text}")

        stored_names = []
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            # 条件更新领取进度：只有真正抢到这段产量的请求才能发奖，防止并发双领
            if is_owner:
                cursor = await self.db.conn.execute(
                    "UPDATE mine_veins SET owner_claimed_hour = ? "
                    "WHERE id = ? AND owner_claimed_hour = ?",
                    (end_hour, mine_id, last_hour),
                )
            else:
                cursor = await self.db.conn.execute(
                    "UPDATE mine_partners SET last_claim_hour = ? "
                    "WHERE mine_id = ? AND user_id = ? AND last_claim_hour = ?",
                    (end_hour, mine_id, player.user_id, last_hour),
                )
            if not cursor.rowcount:
                await self.db.conn.rollback()
                return False, "⏳ 这段产量已被结算，请稍后再来开采。", {}
            # 全字段回写前必须在事务内重读灵石：player 是命令开始时的快照，
            # 而后台暗标退款/盘口派彩并不持玩家锁。旧写法 `player.gold += total_gold`
            # 再 update_player，会把这期间刚入账的灵石整笔抹掉。
            fresh = await self._fetchone(
                "SELECT gold FROM players WHERE user_id = ?", (player.user_id,)
            )
            base_gold = int((fresh or {}).get("gold") or 0) if fresh else int(player.gold or 0)
            player.gold = base_gold + total_gold
            for name, qty in material_total.items():
                try:
                    ok, _ = await self.storage_ring_mgr.store_item(
                        player, name, qty, silent=True, external_transaction=True
                    )
                    if ok:
                        stored_names.append(f"{name}x{qty}")
                except Exception as exc:
                    logger.warning(f"【矿脉】伴生材料发放失败 {name}: {exc}")
            await self.db.update_player(player, commit=False)
            await self.db.conn.commit()
        except Exception:
            await self.db.conn.rollback()
            raise
        return True, "", {
            "mine_id": mine_id,
            "hours": hours,
            "share": share,
            "gold": total_gold,
            "materials": stored_names,
            "events": event_lines,
        }

    async def claim(self, player: Player, mine_id: int) -> Tuple[bool, str]:
        mine = await self._fetchone("SELECT * FROM mine_veins WHERE id = ?", (mine_id,))
        if not mine:
            return False, "❌ 矿脉不存在。"
        if mine["status"] != "active":
            return False, "✅ 该矿脉已到期，剩余产量已自动结算入账，无需再手动开采。"
        now = int(time.time())
        is_owner = mine["owner_id"] == player.user_id
        partner = await self._fetchone(
            "SELECT * FROM mine_partners WHERE mine_id = ? AND user_id = ? AND status = 'accepted'",
            (mine_id, player.user_id),
        )
        if not is_owner and not partner:
            return False, "❌ 你既不是矿主也不是已接受的合伙人。"

        ok, err, summary = await self._settle_claim_for(mine, player, is_owner, partner, now)
        if not ok:
            return False, err
        return True, self._format_claim_message(mine_id, summary, player.gold)

    def _format_claim_message(
        self, mine_id: int, summary: dict, gold: int, auto: bool = False
    ) -> str:
        title = "到期自动结算" if auto else "开采结算"
        lines = [
            f"⛏️ 矿脉 {mine_id} {title}",
            "━━━━━━━━━━━━━━━",
            f"结算时长：{summary['hours']} 小时｜你的份额：{summary['share']}%",
            f"灵石收入：+{summary['gold']:,}",
        ]
        if auto:
            lines.append("✅ 剩余产量已自动入账，无需再手动领取。")
        if summary["materials"]:
            lines.append("伴生材料：" + "、".join(summary["materials"]))
        if summary["events"]:
            lines.append("矿脉事件：")
            lines.extend(summary["events"][-5:])
        else:
            lines.append("矿脉事件：无异常")
        lines.extend([
            "━━━━━━━━━━━━━━━",
            f"💰 当前灵石：{gold:,}",
        ])
        return "\n".join(lines)

    async def _origin_for(self, stored_origin: str, user_id: str) -> str:
        """取结算消息的投递会话。

        优先使用购买/接受邀请时记录的 origin；旧矿脉没有该字段时，
        回退到自动代理已保存的 origin，保持与自动代理同一条消息链路。
        """
        origin = str(stored_origin or "")
        if origin:
            return origin
        try:
            row = await self.db.ext.get_auto_proxy(user_id)
        except Exception:
            row = None
        return str((row or {}).get("origin") or "")

    async def settle_expired(self, now: Optional[int] = None) -> int:
        """到期自动结算：矿脉到期后把矿主与已接受合伙人的剩余产量补发入账。

        后台维护任务每轮调用一次。返回本轮真正完成结算（并置为 expired）的矿脉条数。
        单条矿脉结算失败只记录日志，下一轮会重试，不会让未结算产量凭空消失。
        """
        now = int(now or time.time())
        mines = await self._fetchall(
            "SELECT * FROM mine_veins WHERE status = 'active' AND expire_at <= ? ORDER BY id",
            (now,),
        )
        settled = 0
        for mine in mines:
            try:
                if await self._auto_settle_mine(mine, now):
                    settled += 1
            except Exception as exc:
                logger.error(
                    f"【矿脉】到期自动结算失败 mine_id={mine.get('id')}: {exc}", exc_info=True
                )
        return settled

    async def _auto_settle_mine(self, mine: dict, now: int) -> bool:
        mine_id = int(mine["id"])
        payouts: List[str] = []

        owner = await self.db.get_player_by_id(mine["owner_id"])
        if owner:
            ok, _msg, summary = await self._settle_claim_for(mine, owner, True, None, now)
            if ok:
                if summary.get("gold"):
                    payouts.append(f"矿主 {mine['owner_id']} +{int(summary['gold']):,}")
                if summary.get("gold") or summary.get("materials"):
                    origin = await self._origin_for(mine.get("origin", ""), mine["owner_id"])
                    await self._notify(
                        origin,
                        self._format_claim_message(mine_id, summary, owner.gold, auto=True),
                        mine["owner_id"],
                    )

        partners = await self._fetchall(
            "SELECT * FROM mine_partners WHERE mine_id = ? AND status = 'accepted'", (mine_id,)
        )
        for row in partners:
            partner = await self.db.get_player_by_id(row["user_id"])
            if not partner:
                continue
            ok, _msg, summary = await self._settle_claim_for(mine, partner, False, row, now)
            if ok:
                if summary.get("gold"):
                    payouts.append(f"合伙人 {row['user_id']} +{int(summary['gold']):,}")
                if summary.get("gold") or summary.get("materials"):
                    origin = await self._origin_for(row.get("origin", ""), row["user_id"])
                    await self._notify(
                        origin,
                        self._format_claim_message(mine_id, summary, partner.gold, auto=True),
                        row["user_id"],
                    )

        # 只有仍处于 active 时才置为 expired，避免并发下重复统计/重复派彩
        cursor = await self.db.conn.execute(
            "UPDATE mine_veins SET status = 'expired' WHERE id = ? AND status = 'active'",
            (mine_id,),
        )
        await self.db.conn.commit()
        if not cursor.rowcount:
            return False
        if payouts:
            logger.info(f"【矿脉】矿脉 {mine_id} 到期自动结算：" + "；".join(payouts))
        return True
