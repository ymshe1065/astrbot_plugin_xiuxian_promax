# managers/social_manager.py
"""社交系统 v6.16 - NPC道侣（仙灵结缘）+ 师徒系统"""
import json
import time
from typing import Tuple, Optional, Dict

from ..data.data_manager import DataBase
from ..models import Player

__all__ = ["SocialManager", "SPIRIT_COMPANIONS"]


# ===== 五位仙灵（NPC道侣）=====
SPIRIT_COMPANIONS = {
    "青璃": {
        "title": "丹谷仙子",
        "desc": "青丘丹谷传人，温婉聪慧，精于丹道。",
        "perk": "炼丹成功率 +5%",
        "perk_type": "alchemy",       # 炼丹
        "bonus_max": 0.05,
    },
    "白芷": {
        "title": "剑冢侍剑",
        "desc": "剑冢守剑人，冷若冰霜，剑意冲霄。",
        "perk": "历练修为收益 +8%（满级）",
        "perk_type": "adventure",     # 历练
        "bonus_max": 0.08,
    },
    "绯烟": {
        "title": "镖行大小姐",
        "desc": "天下镖行独女，飒爽仗义，交游广阔。",
        "perk": "押镖镖银 +12%（满级）",
        "perk_type": "escort",        # 押镖
        "bonus_max": 0.12,
    },
    "玄霜": {
        "title": "北冥散人",
        "desc": "北冥之滨的神秘散修，闭关千年，道行深不可测。",
        "perk": "闭关修为 +6%（满级）",
        "perk_type": "cultivate",     # 闭关
        "bonus_max": 0.06,
    },
    "灵汐": {
        "title": "灵兽谷少主",
        "desc": "灵兽谷少主，通兽语，怀中总趴着一只雪貂。",
        "perk": "灵兽战斗属性 +10%（满级）",
        "perk_type": "pet",           # 灵兽
        "bonus_max": 0.10,
    },
}

COMPANION_COST = 50000        # 结缘费用
COMPANION_MIN_LEVEL = 3       # 结缘最低境界
DAILY_BOND_EXP = 1500         # 每日共修修为（基于双方）
BOND_PER_DAY = 8              # 每日共修情缘值
BOND_PER_LEVEL = 100          # 每级情缘所需（NPC等级 = bond // 100 + 1）
MAX_COMPANION_LEVEL = 10


class SocialManager:
    """NPC道侣 + 师徒管理器"""

    def __init__(self, db: DataBase):
        self.db = db

    # ================= NPC 道侣 =================

    async def get_companion(self, user_id: str) -> Optional[Dict]:
        async with self.db.conn.execute(
            "SELECT * FROM npc_partners WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
            return None

    async def has_blessed_land(self, user_id: str) -> bool:
        async with self.db.conn.execute(
            "SELECT 1 FROM blessed_lands WHERE user_id = ? LIMIT 1", (user_id,)
        ) as cursor:
            return await cursor.fetchone() is not None

    async def require_blessed_land(self, user_id: str) -> Tuple[bool, str]:
        if await self.has_blessed_land(user_id):
            return True, ""
        return False, "❌ 仙灵需栖息于洞天。请先使用 /购买洞天 1 开辟洞天，再与仙灵结缘。"

    async def list_companions(self) -> str:
        lines = ["💍 仙灵结缘", "━━━━━━━━━━━━━━━"]
        for name, c in SPIRIT_COMPANIONS.items():
            lines.append(f"【{name}】· {c['title']}")
            lines.append(f"  {c['desc']}")
            lines.append(f"  缘加成：{c['perk']}")
        lines.append("━━━━━━━━━━━━━━━")
        lines.append(
            f"结缘条件：拥有洞天，境界≥{COMPANION_MIN_LEVEL}，{COMPANION_COST:,} 灵石，且无真人双修请求进行中\n"
            "💡 仙灵栖息于洞天：结缘/共修/代收均需洞天 · /仙灵解除 解缘"
        )
        return "\n".join(lines)

    async def bond_companion(self, player: Player, name: str) -> Tuple[bool, str]:
        """结缘仙灵"""
        cfg = SPIRIT_COMPANIONS.get(name)
        if not cfg:
            return False, f"❌ 没有名为【{name}】的仙灵。发送 /仙灵结缘 查看全部仙灵。"
        if player.level_index < COMPANION_MIN_LEVEL:
            return False, f"❌ 结缘仙灵需要达到境界等级 {COMPANION_MIN_LEVEL}（当前 {player.level_index}）。"
        existing = await self.get_companion(player.user_id)
        if existing:
            old_name = SPIRIT_COMPANIONS.get(existing["npc_id"], {}).get("title", existing["npc_id"])
            return False, f"❌ 你已与【{existing['npc_id']}】（{old_name}）结缘。先 /仙灵解除 再结新缘。"
        land_ok, land_msg = await self.require_blessed_land(player.user_id)
        if not land_ok:
            return False, land_msg
        # 与真人双修互斥：有待处理的双修请求（作为发起者）时不可结缘
        async with self.db.conn.execute(
            "SELECT COUNT(*) FROM dual_cultivation_requests WHERE from_id = ?", (player.user_id,)
        ) as cursor:
            cnt = (await cursor.fetchone())[0]
        if cnt > 0:
            return False, "❌ 你有未处理的双修请求，请先 /拒绝双修 或等对方回应，再考虑仙灵结缘。"
        if player.gold < COMPANION_COST:
            return False, f"❌ 结缘需要 {COMPANION_COST:,} 灵石（三书六礼总要有的）。"

        player.gold -= COMPANION_COST
        await self.db.update_player(player)
        now = int(time.time())
        await self.db.conn.execute(
            "INSERT OR REPLACE INTO npc_partners (user_id, npc_id, bond, daily_time, created_at) VALUES (?, ?, 0, 0, ?)",
            (player.user_id, name, now)
        )
        await self.db.conn.commit()
        return True, (
            f"💍 结缘成功！\n"
            f"━━━━━━━━━━━━━━━\n"
            f"你与【{name}】（{cfg['title']}）结为道侣！\n"
            f"「{cfg['desc']}」\n"
            f"缘加成：{cfg['perk']}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💡 每日 /仙灵共修 培养情缘，情缘等级提升加成（最高 Lv.{MAX_COMPANION_LEVEL}）"
        )

    async def daily_commune(self, player: Player) -> Tuple[bool, str]:
        """仙灵共修（每日一次，得修为+情缘）"""
        comp = await self.get_companion(player.user_id)
        if not comp:
            return False, "❌ 你还没有仙灵道侣。发送 /仙灵结缘 查看名单。"
        land_ok, land_msg = await self.require_blessed_land(player.user_id)
        if not land_ok:
            return False, land_msg.replace("结缘", "共修")
        now = int(time.time())
        last = comp["daily_time"]
        if now - last < 86400 and time.strftime("%Y-%m-%d", time.localtime(last)) == time.strftime("%Y-%m-%d"):
            return False, "❌ 今日已与仙灵共修过了，明日再来（一日不见如隔三秋）。"

        cfg = SPIRIT_COMPANIONS.get(comp["npc_id"], {})
        level = min(MAX_COMPANION_LEVEL, comp["bond"] // BOND_PER_LEVEL + 1)
        exp_gain = DAILY_BOND_EXP * (1 + player.level_index // 5) * (1 + level * 0.1)
        exp_gain = int(exp_gain)
        new_bond = comp["bond"] + BOND_PER_DAY
        new_level = min(MAX_COMPANION_LEVEL, new_bond // BOND_PER_LEVEL + 1)

        player.experience += exp_gain
        await self.db.update_player(player)
        await self.db.conn.execute(
            "UPDATE npc_partners SET bond = ?, daily_time = ? WHERE user_id = ?",
            (new_bond, now, player.user_id)
        )
        await self.db.conn.commit()

        up_note = ""
        if new_level > level:
            up_note = f"\n🌸 情缘升温！{comp['npc_id']} 的缘等级提升至 Lv.{new_level}（{cfg.get('perk', '加成')}更强了）"
        return True, (
            f"💞 仙灵共修\n"
            f"━━━━━━━━━━━━━━━\n"
            f"你与【{comp['npc_id']}】（Lv.{level}）静坐通宵，双修一夜。\n"
            f"获得修为：+{exp_gain:,}\n"
            f"情缘：{comp['bond']} → {new_bond}"
            f"{up_note}"
        )

    async def break_companion(self, player: Player) -> Tuple[bool, str]:
        comp = await self.get_companion(player.user_id)
        if not comp:
            return False, "❌ 你没有仙灵道侣。"
        await self.db.conn.execute("DELETE FROM npc_partners WHERE user_id = ?", (player.user_id,))
        await self.db.conn.commit()
        return True, (
            f"💔 你与【{comp['npc_id']}】的缘分散了。\n"
            f"情缘值已清零，结缘礼金不退。\n"
            f"（仙灵不语，转身化作青烟散去……）"
        )

    # ===== v6.17 道侣洞天共享（仙灵代收）=====

    async def companion_collect_land(self, player: Player) -> Tuple[bool, str]:
        """仙灵代收洞天产出：道侣共享洞天，仙灵替你收取闲置产出。
        情谊分成：代收灵石的 20% 留给仙灵（消耗情缘值 1 点/次），其余归玩家。"""
        comp = await self.get_companion(player.user_id)
        if not comp:
            return False, "❌ 你没有仙灵道侣，无法代收。发送 /仙灵结缘 结缘一位仙灵。"
        if comp["bond"] <= 0:
            return False, "❌ 情缘值不足（需 ≥1），先 /仙灵共修 培养情缘。"

        land = None
        async with self.db.conn.execute(
            "SELECT * FROM blessed_lands WHERE user_id = ?", (player.user_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return False, "❌ 你还没有洞天！先 /购买洞天 1。"
        land = dict(row)

        now = int(time.time())
        hours_passed = (now - land["last_collect_time"]) / 3600
        if hours_passed < 1:
            remaining = int(3600 - (now - land["last_collect_time"]))
            return False, f"❌ 洞天暂无余粮（收取冷却 {remaining // 60} 分钟）。仙灵说：刚收过啦，无物可代收～"

        hours = min(24, int(hours_passed))
        credit_start = max(land["last_collect_time"], now - hours * 3600)
        output_mult = player.shield_output_multiplier(credit_start, now)
        gold_income = land["gold_per_hour"] * hours
        # 灵泉假山加成
        try:
            furniture = json.loads(land.get("furniture") or "[]")
            if "灵泉假山" in furniture:
                gold_income = int(gold_income * 1.10)
        except Exception:
            pass
        gold_income = int(gold_income * output_mult)

        spirit_cut = int(gold_income * 0.20)
        player_cut = gold_income - spirit_cut

        # 洞天产出进洞天主人账上；仙灵消耗 1 点情缘作为谢礼
        await self.db.add_player_gold(player.user_id, player_cut)
        await self.db.conn.execute(
            "UPDATE blessed_lands SET last_collect_time = ? WHERE user_id = ?",
            (now, player.user_id)
        )
        await self.db.conn.execute(
            "UPDATE npc_partners SET bond = bond - 1 WHERE user_id = ?",
            (player.user_id,)
        )
        await self.db.conn.commit()

        cfg = SPIRIT_COMPANIONS.get(comp["npc_id"], {})
        shield_note = ""
        if output_mult < 1.0:
            penalty_pct = 10 if getattr(player, "life_skill", "") == "阵法师" else 20
            shield_note = f"\n⚠️ 护府大阵生效期间，代收灵石按 -{penalty_pct}% 折算"
        return True, (
            f"🏔️【仙灵代收】\n"
            f"━━━━━━━━━━━━━━━\n"
            f"道侣【{comp['npc_id']}】（{cfg.get('title', '')}）代你巡视洞天，\n"
            f"收得闲置产出 {hours} 小时：\n"
            f"💰 灵石：+{player_cut:,}（仙灵情谊分成 {spirit_cut:,}）\n"
            f"💞 消耗情缘 1 点（剩余 {comp['bond'] - 1}）"
            f"{shield_note}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💡 洞天已重新计时，/洞天收取 正常收取不受影响"
        )

    def companion_bonus(self, user_id: str, perk_type: str) -> float:
        """获取玩家仙灵的某类加成倍率（如 0.05 = +5%）。无仙灵或类型不符返回 0"""
        # 同步版本不查库：由调用方在有 comp 时传入；这里简化为查内存缓存的上层接口
        return 0.0

    async def companion_bonus_async(self, user_id: str, perk_type: str) -> float:
        comp = await self.get_companion(user_id)
        if not comp:
            return 0.0
        cfg = SPIRIT_COMPANIONS.get(comp["npc_id"], {})
        if cfg.get("perk_type") != perk_type:
            return 0.0
        level = min(MAX_COMPANION_LEVEL, comp["bond"] // BOND_PER_LEVEL + 1)
        base = 0.05
        cap = float(cfg.get("bonus_max", base) or base)
        if cap <= base:
            return base
        return base + (cap - base) * (level - 1) / max(1, MAX_COMPANION_LEVEL - 1)

    # ================= 师徒系统 =================

    MAX_DISCIPLES = 2  # 每位师父同门在学弟子上限（防小号）

    async def get_master_of(self, disciple_id: str) -> Optional[Dict]:
        async with self.db.conn.execute(
            "SELECT * FROM master_relations WHERE disciple_id = ? AND graduated = 0", (disciple_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_disciples(self, master_id: str) -> list:
        async with self.db.conn.execute(
            "SELECT * FROM master_relations WHERE master_id = ? AND graduated = 0", (master_id,)
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

    async def take_as_disciple(self, disciple: Player, master_id: str) -> Tuple[bool, str]:
        """拜师"""
        if disciple.user_id == master_id:
            return False, "❌ 不能拜自己为师。"
        if disciple.level_index >= 10:
            return False, "❌ 境界已过高（≥10级），无缘再拜师学艺。"
        master = await self.db.get_player_by_id(master_id)
        if not master:
            return False, "❌ 对方还未踏入修仙之路。"
        if master.level_index < disciple.level_index + 5:
            return False, f"❌ 拜师需对方境界高于你 5 级以上（对方 {master.level_index} 级，你 {disciple.level_index} 级）。"
        existing = await self.get_master_of(disciple.user_id)
        if existing:
            m = await self.db.get_player_by_id(existing["master_id"])
            mname = m.user_name if m and m.user_name else existing["master_id"][:8]
            return False, f"❌ 你已有师父【{mname}】。"
        count = len(await self.get_disciples(master_id))
        if count >= self.MAX_DISCIPLES:
            return False, f"❌ 对方门下弟子已满（{self.MAX_DISCIPLES} 人），暂时不再收徒。"

        now = int(time.time())
        await self.db.conn.execute(
            "INSERT INTO master_relations (master_id, disciple_id, created_at, graduated) VALUES (?, ?, ?, 0)",
            (master_id, disciple.user_id, now)
        )
        await self.db.conn.commit()
        return True, (
            f"🍚 拜师成功！\n"
            f"━━━━━━━━━━━━━━━\n"
            f"你跪奉茶盏，正式拜【{master.user_name or master_id[:8]}】为师！\n"
            f"💡 师父在你每次突破时获得师德点（可在 /师德商店 兑换奖励）\n"
            f"💡 当你达到筑基期（境界3）后，师父可用 /出师 为你出师，双方获大礼"
        )

    async def on_disciple_breakthrough(self, disciple_id: str) -> Tuple[bool, str]:
        """徒弟突破时调用：师父获得师德点。返回提示（供拼入突破消息）"""
        rel = await self.get_master_of(disciple_id)
        if not rel:
            return False, ""
        gain = 5
        await self.db.conn.execute(
            "UPDATE players SET virtue_points = virtue_points + ? WHERE user_id = ?",
            (gain, rel["master_id"])
        )
        await self.db.conn.commit()
        master = await self.db.get_player_by_id(rel["master_id"])
        if not master:
            return False, ""
        mname = master.user_name or rel["master_id"][:8]
        return True, f"\n🎓 你的突破令师父【{mname}】欣喜，师德 +{gain}（当前 {master.virtue_points + gain}）"

    async def graduate_disciple(self, master: Player, disciple_id: str) -> Tuple[bool, str]:
        """出师（师父操作，徒弟需境界≥3）"""
        rel = await self.get_master_of(disciple_id)
        if not rel or rel["master_id"] != master.user_id:
            return False, "❌ 对方不是你的在学弟子。"
        d = await self.db.get_player_by_id(disciple_id)
        if not d:
            return False, "❌ 弟子数据异常。"
        if d.level_index < 3:
            dn = d.user_name or disciple_id[:8]
            return False, f"❌ 弟子【{dn}】尚未达到筑基期（境界3），尚需打磨。"
        # 奖励双方
        d_reward_gold, d_reward_exp = 30000, 20000
        m_reward_gold = 20000
        d.gold += d_reward_gold
        d.experience += d_reward_exp
        d.fate_points = getattr(d, "fate_points", 0) + 20
        await self.db.update_player(d)
        await self.db.add_player_gold(master.user_id, m_reward_gold)
        await self.db.conn.execute(
            "UPDATE master_relations SET graduated = 1 WHERE id = ?", (rel["id"],)
        )
        await self.db.conn.commit()
        dn = d.user_name or disciple_id[:8]
        return True, (
            f"🎓 出师礼成！\n"
            f"━━━━━━━━━━━━━━━\n"
            f"弟子【{dn}】已至筑基，学有所成！\n"
            f"弟子获得：{d_reward_gold:,} 灵石 + {d_reward_exp:,} 修为 + 20 仙缘点\n"
            f"你获得：{m_reward_gold:,} 灵石（传道之酬）\n"
            f"师徒缘分长存，自此江湖再见亦是故人。"
        )

    async def dismiss_disciple(self, master: Player, disciple_id: str) -> Tuple[bool, str]:
        """逐出师门"""
        rel = await self.get_master_of(disciple_id)
        if not rel or rel["master_id"] != master.user_id:
            return False, "❌ 对方不是你的在学弟子。"
        await self.db.conn.execute("DELETE FROM master_relations WHERE id = ?", (rel["id"],))
        await self.db.conn.commit()
        d = await self.db.get_player_by_id(disciple_id)
        dn = d.user_name if d and d.user_name else disciple_id[:8]
        return True, f"✂️ 你已将【{dn}】逐出师门。师徒恩断义绝。"

    async def my_master_info(self, player: Player) -> str:
        """我的师徒关系面板"""
        lines = ["🎓 师徒缘法", "━━━━━━━━━━━━━━━"]
        rel = await self.get_master_of(player.user_id)
        if rel:
            m = await self.db.get_player_by_id(rel["master_id"])
            mname = m.user_name if m and m.user_name else rel["master_id"][:8]
            lines.append(f"你的师父：【{mname}】")
        else:
            if player.level_index >= 10:
                lines.append("你的境界已高，无缘拜师。")
            else:
                lines.append("你尚未拜师。💡 /拜师 @高境界道友")
        disciples = await self.get_disciples(player.user_id)
        if disciples:
            lines.append(f"你的在学弟子（{len(disciples)}/{self.MAX_DISCIPLES}）：")
            for drel in disciples:
                d = await self.db.get_player_by_id(drel["disciple_id"])
                dn = d.user_name if d and d.user_name else drel["disciple_id"][:8]
                lines.append(f"  · 【{dn}】(境界{d.level_index if d else '?'})")
        else:
            lines.append("你门下暂无弟子。💡 等后辈 /拜师 @你")
        vp = getattr(player, "virtue_points", 0)
        lines.append(f"师德点：{vp}")
        lines.append("━━━━━━━━━━━━━━━")
        lines.append("师德商店：/师德兑换 1 引雷符(80) · /师德兑换 2 空间石x2(150) · /师德兑换 3 称号【传道真人】(300)")
        return "\n".join(lines)

    async def redeem_virtue(self, player: Player, item_id: int) -> Tuple[bool, str]:
        """师德商店兑换"""
        shop = {
            1: {"name": "引雷符", "cost": 80, "type": "item", "item": "引雷符", "count": 1},
            2: {"name": "空间石 x2", "cost": 150, "type": "item", "item": "空间石", "count": 2},
            3: {"name": "称号【传道真人】", "cost": 300, "type": "title", "title": "传道真人"},
        }
        goods = shop.get(item_id)
        if not goods:
            return False, "❌ 无效的兑换编号。可选：1 引雷符(80) / 2 空间石x2(150) / 3 称号传道真人(300)"
        vp = getattr(player, "virtue_points", 0)
        if vp < goods["cost"]:
            return False, f"❌ 师德点不足！需要 {goods['cost']}，当前 {vp}。\n💡 收徒并等弟子 /突破 即可累积师德。"
        if goods["type"] == "item" and not getattr(player, "_storage_ref", None):
            pass  # 储物戒由 handler 层传入处理
        if goods["type"] == "title":
            from .title_manager import TitleManager
            tm = TitleManager(self.db)
            title_name = goods["title"]
            if tm.has_title(player, title_name):
                return False, "❌ 你已拥有该称号。"
            player.virtue_points = vp - goods["cost"]
            is_new, title_msg = tm.grant(player, title_name)
            if not is_new:
                return False, "❌ 称号授予失败，请稍后再试。"
            if not player.title:
                player.title = title_name
            await self.db.update_player(player)
            return True, (
                f"🏅 兑换成功！{title_msg.strip()}\n"
                f"消耗师德 {goods['cost']}，剩余 {player.virtue_points}"
            )
        player.virtue_points = vp - goods["cost"]
        await self.db.update_player(player)
        return True, f"__NEED_STORE__{goods['item']}|{goods['count']}|兑换【{goods['name']}】成功（消耗师德 {goods['cost']}，剩余 {player.virtue_points}）"
