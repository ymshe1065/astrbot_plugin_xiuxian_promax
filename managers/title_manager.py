# managers/title_manager.py
"""称号系统 v6.17 - 110 称号六品阶，最高品阶生效加成"""
import json
import time
from typing import Tuple, Optional, Dict

from astrbot.api import logger
from ..data.data_manager import DataBase
from ..models import Player
from .title_data import TITLES, TIERS

__all__ = ["TitleManager"]

# 称号档案存于 permanent_bonus["titles"]: {称号名: 获得时间戳}
# 当前佩戴称号存于 player.title（展示用）；加成按图鉴内最高品阶自动生效

class TitleManager:
    def __init__(self, db: DataBase):
        self.db = db
        self.titles: Dict[str, dict] = {t["name"]: t for t in TITLES}

    def _load_owned(self, player: Player) -> Dict[str, float]:
        try:
            data = json.loads(player.permanent_bonus) if player.permanent_bonus else {}
            if not isinstance(data, dict):
                return {}
            owned = data.get("titles", {})
            return owned if isinstance(owned, dict) else {}
        except Exception:
            return {}

    def _save_owned(self, player: Player, owned: Dict[str, float]):
        try:
            data = json.loads(player.permanent_bonus) if player.permanent_bonus else {}
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        data["titles"] = owned
        player.permanent_bonus = json.dumps(data, ensure_ascii=False)

    def grant(self, player: Player, title_name: str) -> Tuple[bool, str]:
        """授予称号（幂等：已拥有则跳过）。返回 (是否新获得, 消息)"""
        t = self.titles.get(title_name)
        if not t:
            return False, ""
        owned = self._load_owned(player)
        if title_name in owned:
            return False, ""
        owned[title_name] = float(time.time())
        self._save_owned(player, owned)
        tier_name = TIERS[t["tier"]][0]
        return True, f"\n🏅 获得称号【{title_name}】（{tier_name}）！已录入称号图鉴。"

    def grant_by_key(self, player: Player, key: str, value) -> str:
        """按获取路径 key 批量检查并授予。返回提示文本（可能为空）"""
        msgs = []
        owned = self._load_owned(player)
        for t in TITLES:
            if t["how"] != key or t["name"] in owned:
                continue
            cond = t.get("condition")
            if cond is None:
                continue
            try:
                if key == "karma_neg":
                    met = (player.karma or 0) <= cond
                elif isinstance(value, (int, float)):
                    met = value >= cond
                else:
                    met = False
            except Exception:
                met = False
            if met:
                ok, msg = self.grant(player, t["name"])
                if ok:
                    msgs.append(msg)
        return "".join(msgs)

    # ================= 新增：历史称号自动补齐 =================
    async def sync_missing_titles(self, player: Player) -> str:
        """玩家查询时自动校准历史数据，补发遗漏的称号"""
        owned = self._load_owned(player)
        if len(owned) >= len(TITLES):
            return ""  # 全都有了，无需校验

        newly_granted = []
        
        # 1. 提前获取需要查外部表的属性
        pet_level = 0
        land_level = 0
        
        try:
            async with self.db.conn.execute("SELECT MAX(level) FROM player_pets WHERE user_id = ?", (player.user_id,)) as cur:
                row = await cur.fetchone()
                if row and row[0]: pet_level = row[0]
        except Exception: pass
        
        try:
            async with self.db.conn.execute("SELECT level FROM blessed_lands WHERE user_id = ?", (player.user_id,)) as cur:
                row = await cur.fetchone()
                if row: land_level = row[0]
        except Exception: pass

        # 2. 遍历所有称号，判断是否遗漏
        for t in TITLES:
            if t["name"] in owned: continue
            how = t["how"]
            cond = t.get("condition")
            
            # 无条件的（如手动授予、满级等）不参与自动补齐
            if cond is None: continue
            
            met = False
            try:
                # 玩家自身属性类
                if how == "breakthrough":
                    met = getattr(player, "level_index", 0) >= cond
                elif how == "karma":
                    met = getattr(player, "karma", 0) >= cond
                elif how == "karma_neg":
                    met = getattr(player, "karma", 0) <= cond
                elif how == "gold_reach":
                    met = getattr(player, "gold", 0) >= cond
                elif how == "reincarnation":
                    met = getattr(player, "reincarnation_count", 0) >= cond
                elif how == "pet_level":
                    met = pet_level >= cond
                elif how == "land_level":
                    met = land_level >= cond
                elif how == "furnace_max":
                    # 补偿历史满级玩家：升级命令在满级时会直接失败，无法再触发补发
                    try:
                        bonus = json.loads(player.permanent_bonus) if player.permanent_bonus else {}
                    except Exception:
                        bonus = {}
                    met = int(bonus.get("alchemy_furnace", 0) or 0) >= cond

                # 外部事件累进类：读 system_config
                elif how not in ("manual", "furnace_max"):
                    ck = f"title_cnt_{how}_{player.user_id}"
                    raw = await self.db.ext.get_system_config(ck)
                    val = int(raw) if raw and raw.isdigit() else 0
                    met = val >= cond
                    
            except Exception:
                met = False
                
            if met:
                ok, _ = self.grant(player, t["name"])
                if ok:
                    newly_granted.append(t["name"])

        # 3. 如果有补发，落库并返回提示
        if newly_granted:
            await self.db.update_player(player)
            msg = f"\n🔄 【天道纠察】检测到你历史满足条件但遗漏的称号，已自动为你补发 {len(newly_granted)} 个！"
            return msg
            
        return ""
    # =========================================================

    @staticmethod
    def _pick_active_title(
        owned: Dict, title_map: Dict[str, dict], worn: str = ""
    ) -> Optional[dict]:
        """统一“当前生效称号”规则，保证展示与各类加成口径一致。

        优先级：最高品阶 → 同品阶优先当前佩戴称号 → 再取最近获得的称号。
        旧实现仅按字典插入顺序取首个同品阶称号，导致“图鉴显示生效的称号”
        与实际生效加成可能不是同一个。
        """
        if not isinstance(owned, dict) or not owned:
            return None
        candidates = []
        for name, obtained in owned.items():
            t = title_map.get(name)
            if not t:
                continue
            try:
                ts = float(obtained or 0)
            except (TypeError, ValueError):
                ts = 0.0
            candidates.append((t, ts))
        if not candidates:
            return None
        best_tier = max(t["tier"] for t, _ in candidates)
        same_tier = [(t, ts) for t, ts in candidates if t["tier"] == best_tier]
        if worn:
            for t, _ in same_tier:
                if t["name"] == worn:
                    return t
        same_tier.sort(key=lambda item: item[1], reverse=True)
        return same_tier[0][0]

    def get_active_bonus(self, player: Player) -> Dict:
        """计算当前生效的加成（最高品阶的一个称号）"""
        active = self.get_active_title(player)
        return active["stats"] if active else {}

    @staticmethod
    def get_exp_bonus(player: Player) -> float:
        """当前最高品阶称号提供的修为收益倍率。"""
        try:
            return float(TitleManager(None).get_active_bonus(player).get("exp", 0) or 0)
        except Exception:
            return 0.0

    def has_title(self, player: Player, title_name: str) -> bool:
        return title_name in self._load_owned(player)

    def get_active_title(self, player: Player) -> Optional[dict]:
        """当前提供加成的称号（统一规则，供气运/修为/战斗/灵石共用）。"""
        return self._pick_active_title(
            self._load_owned(player),
            self.titles,
            getattr(player, "title", "") or "",
        )

    def equip_display(self, player: Player, title_name: str) -> Tuple[bool, str]:
        """佩戴称号（支持空参数自动佩戴最高品阶，支持摘下）"""
        owned = self._load_owned(player)
        
        # 容错处理：去除首尾空格和 @ 符号
        clean_name = title_name.strip().lstrip("@") if title_name else ""
        
        # 如果没填名字，自动佩戴最高品阶
        if not clean_name:
            best = self.get_active_title(player)
            if not best:
                return False, "❌ 你尚未拥有任何称号。"
            player.title = best["name"]
            return True, f"✅ 已自动为你佩戴最高品阶称号【{best['name']}】。"

        if clean_name == "无" or clean_name == "摘下":
            player.title = ""
            return True, "已摘下称号。"

        # 容错匹配
        if clean_name not in owned:
            matched = [k for k in owned if k == clean_name or k.strip() == clean_name]
            if not matched:
                return False, f"❌ 你尚未拥有称号【{clean_name}】。发送 /称号图鉴 查看图鉴"
            clean_name = matched[0]

        player.title = clean_name
        return True, f"已佩戴称号【{clean_name}】。"

    def collection_view(self, player: Player) -> str:
        """称号图鉴"""
        owned = self._load_owned(player)
        active = self.get_active_title(player)
        
        valid_owned_count = len([n for n in owned if n in self.titles])
        lines = [f"📜 称号图鉴（已收集 {valid_owned_count}/{len(TITLES)}）", "━━━━━━━━━━━━━━━"]
        
        by_tier = {}
        for t in TITLES:
            by_tier.setdefault(t["tier"], []).append(t)
            
        for tier in sorted(by_tier):
            tier_name = TIERS.get(tier, (f"未知({tier})",))[0]
            lines.append(f"【{tier_name}】")
            for t in by_tier[tier]:
                mark = "✅" if t["name"] in owned else "🔒"
                active_mark = " ⭐生效中" if active and active["name"] == t["name"] else ""
                how_txt = self._how_text(t)
                stats_txt = self._format_stats(t["stats"], sep="/")
                lines.append(f"  {mark}【{t['name']}】{how_txt}｜{stats_txt}{active_mark}")

        lines.append("━━━━━━━━━━━━━━━")
        lines.append("💡 每行“｜”后为称号实际加成；前面文字是解锁条件，不是加成效果。")
        lines.append("💡 加成自动按已拥有的最高品阶生效；同品阶优先当前佩戴，否则取最近获得。")
        lines.append("💡 发送 /佩戴称号 <名> 更换展示，或 /佩戴称号 自动佩戴最高品阶。")
        return "\n".join(lines)

    def _how_text(self, t: dict) -> str:
        key = t["how"]
        cond = t.get("condition")
        texts = {
            "breakthrough": f"突破至境界{cond}",
            "win_pvp": f"决斗获胜{cond}场",
            "spar_count": f"切磋{cond}次",
            "checkin_count": f"累计签到{cond}天",
            "boss_damage": f"讨伐世界Boss {cond}次",
            "boss_kill_old": f"击杀旧Boss {cond}次",
            "escort_count": f"完成押镖{cond}次",
            "karma": f"功德达到{cond}",
            "karma_neg": f"业力达到{cond}",
            "craft_perfect": f"炼出极品丹{cond}枚",
            "craft_count": f"炼丹{cond}次",
            "enhance_count": f"强化{cond}次",
            "wash_count": f"洗练{cond}次",
            "gamble_count": f"切原石{cond}次",
            "harvest_count": f"收获灵田{cond}次",
            "cultivate_count": f"闭关{cond}次",
            "adventure_count": f"历练{cond}次",
            "rift_count": f"探索秘境{cond}次",
            "buy_count": f"购买{cond}次",
            "market_sell": f"坊市上架{cond}次",
            "blackmarket_buy": f"黑市购物{cond}次",
            "shield_count": f"开启护府{cond}次",
            "forge_count": f"强化/洗练共{cond}次",
            "pet_level": f"灵兽达到Lv.{cond}",
            "land_level": f"洞天达到Lv.{cond}",
            "furnace_max": (f"丹炉达到Lv.{cond}" if cond is not None else "丹炉升至满级"),
            "reincarnation": f"飞升转生{cond}次",
            "rebirth": (f"弃道重修{cond}次" if cond is not None else "弃道重修"),
            "luck_event": f"触发奇遇{cond}次",
            "sect_battle_win": f"大比获胜{cond}场",
            "demon_catch": f"揪出天魔{cond}次",
            "tide_resist": f"参与抵御魔潮{cond}次",
            "gold_reach": (f"灵石达到{cond:,}" if isinstance(cond, int) else "灵石达到一定数额"),
            "manual": "特殊途径获得",
        }
        return texts.get(key, f"途径：{key} {cond if cond is not None else ''}")

    # ==========================================
    # 保留原方法名，防止其他文件调用报错
    # ==========================================
    def bonus_summary(self, player: Player) -> str:
        """【原有方法保留】返回当前生效的最高称号单行摘要（用于玩家面板等）"""
        stats = self.get_active_bonus(player)
        if not stats:
            return "当前无称号加成"
        active = self.get_active_title(player)
        tier_name = TIERS[active["tier"]][0] if active else ""
        return (
            f"【{active['name']}】({tier_name})：{self._format_stats(stats, sep=' ')}"
            if active
            else "无"
        )

    @classmethod
    def _format_stats(cls, stats: dict, sep: str = " | ") -> str:
        """内部辅助：格式化属性字典为可读文本。

        称号 stats 约定：exp/gold_pct 为小数（0.2=20%），crit/lifesteal
        为整数百分点（5=5%），其余为固定数值。
        """
        if not stats:
            return "无加成"
        parts = []
        cn = {"atk": "物伤", "magic": "法伤", "phys_def": "物防", "magic_def": "法防",
              "crit": "会心", "lifesteal": "吸血", "exp": "修为", "luck": "气运",
              "gold_pct": "灵石"}
        for k, v in stats.items():
            label = cn.get(k, k)
            if k in ("exp", "gold_pct"):
                try:
                    parts.append(f"{label}+{float(v):.0%}")
                except (TypeError, ValueError):
                    parts.append(f"{label}+{v}")
            elif k in ("crit", "lifesteal"):
                parts.append(f"{label}+{v}%")
            else:
                parts.append(f"{label}+{v}")
        return sep.join(parts)

    # ==========================================
    # 新增方法：全量展示与推荐
    # ==========================================
    def my_titles_summary(self, player: Player) -> str:
        """展示玩家已拥有的所有称号，按品阶降序排列"""
        owned = self._load_owned(player)
        if not owned:
            return "📜 你目前尚未获得任何称号，快去探索仙途吧！"
        
        owned_titles = []
        for name, timestamp in owned.items():
            t = self.titles.get(name)
            if t:
                owned_titles.append((t, timestamp))
        
        # 按品阶（高->低）和获得时间（新->旧）排序
        owned_titles.sort(key=lambda x: (x[0]["tier"], x[1]), reverse=True)
        best_title = owned_titles[0][0] if owned_titles else None
        active = self.get_active_title(player)

        lines = [f"🏅 我的称号（已拥有 {len(owned_titles)} 个）", "━━━━━━━━━━━━━━━"]
        for t, _ in owned_titles:
            tier_name = TIERS.get(t["tier"], (f"未知",))[0]
            stats_desc = self._format_stats(t["stats"])

            mark = ""
            if player.title == t["name"]:
                mark = " ⭐当前佩戴"
                if active and active["name"] == t["name"]:
                    mark += "·生效中"
            elif active and active["name"] == t["name"]:
                mark = " ⭐生效中"

            lines.append(f"【{tier_name}】{t['name']}{mark}")
            lines.append(f"  └ {stats_desc}")
            
        lines.append("━━━━━━━━━━━━━━━")
        lines.append("💡 称号加成自动按已拥有的最高品阶生效。")
        
        if best_title and player.title != best_title["name"]:
            lines.append(f"💡 当前推荐佩戴：【{best_title['name']}】")
            lines.append(f"💡 发送 /佩戴称号 {best_title['name']} 可佩戴展示，或直接发送 /佩戴称号 自动佩戴。")
        elif not player.title:
            lines.append(f"💡 发送 /佩戴称号 {best_title['name']} 佩戴展示（或直接发送 /佩戴称号 自动佩戴）。")
            
        return "\n".join(lines)