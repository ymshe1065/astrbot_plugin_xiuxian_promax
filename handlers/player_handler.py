# handlers/player_handler.py
import time
import random
from datetime import datetime
from astrbot.api.event import AstrMessageEvent
from astrbot.api import AstrBotConfig
from ..data import DataBase
from ..core import CultivationManager, PillManager, StorageRingManager
from ..core.reincarnation_rules import (
    FREE_ROOT_REROLLS,
    NEWLIFE_ESCORT_TRIPS_PER_STACK,
    free_root_rerolls,
    permanent_effect,
    NEWLIFE_MARKER,
    NEWLIFE_ROOT_MARKER,
    ROOT_TIER_GUARANTEE_EVERY,
    add_item,
    apply_dao_gift,
    apply_start_level,
    keeps_life_skill,
    keep_gold_ratio,
    consume_root_reroll,
    escort_gold_base,
    format_reroll_budget,
    item_owned,
    ledger_value,
    parse_bonus,
    prev_root_key,
    root_reroll_stock,
    root_roll_key,
)
from ..models import Player
from ..models_extended import UserStatus
from ..config_manager import ConfigManager
from ..managers.tutorial_manager import TutorialManager
from .utils import player_required, with_player_lock, _loan_identity, set_event_flag

CMD_START_XIUXIAN = "我要修仙"
CMD_PLAYER_INFO = "我的信息"
CMD_START_CULTIVATION = "闭关"
CMD_END_CULTIVATION = "出关"
CMD_CHECK_IN = "签到"
REBIRTH_COOLDOWN = 3 * 24 * 3600  # 弃道重修冷却 3 天
# 灵根重抽：每轮 5 次免费（FREE_ROOT_REROLLS），用完后消耗商店的【灵根洗炼】额度
ROOT_REROLL_KEY = "rebirth_root_reroll_{uid}"
CMD_CHOOSE_ROOT = "选择灵根"
PENDING_ROOT_TIMEOUT = 600  # 灵根待选超时 10 分钟

# 七大职业与别名（单字简称、常见写法），入口统一归一化
CULTIVATION_ALIASES = {
    "灵修": "灵修", "灵": "灵修", "法师": "灵修", "灵修者": "灵修",
    "体修": "体修", "体": "体修", "武僧": "体修", "体修者": "体修",
    "魔修": "魔修", "魔": "魔修", "魔修者": "魔修",
    "剑修": "剑修", "剑": "剑修", "剑修者": "剑修", "剑客": "剑修",
    "鬼修": "鬼修", "鬼": "鬼修", "鬼修者": "鬼修",
    "佛修": "佛修", "佛": "佛修", "佛修者": "佛修", "和尚": "佛修",
    "御兽": "御兽", "兽": "御兽", "御兽师": "御兽", "驭兽": "御兽",
}
VALID_CULTIVATIONS = ["灵修", "体修", "魔修", "剑修", "鬼修", "佛修", "御兽"]

# 只在创建角色时展示的职业定位；不写入 /我的信息，避免面板继续膨胀。
CLASS_ROLE_INFO = {
    "灵修": {"code": "balanced_mage", "name": "均衡法系", "desc": "攻防平衡，法术输出稳定，适合新手"},
    "体修": {"code": "physical_tank", "name": "物理坦克", "desc": "防御最高，物伤为主，突破风险较高"},
    "魔修": {"code": "hybrid_dps", "name": "物法综合", "desc": "高攻均衡，物法双修，综合能力强"},
    "剑修": {"code": "physical_dps", "name": "物理爆发", "desc": "攻击最高、防御偏低，追求极限输出"},
    "鬼修": {"code": "magic_dot", "name": "法系输出", "desc": "法术伤害为主，防御略低"},
    "佛修": {"code": "defense_tank", "name": "防御坦克", "desc": "生存能力最强，输出偏低"},
    "御兽": {"code": "pet_summoner", "name": "灵兽召唤", "desc": "多灵兽协同，强度依赖灵兽培养"},
}

__all__ = ["PlayerHandler"]

class PlayerHandler:
    """玩家基础信息处理器 - 支持七大职业（灵/体/魔/剑/鬼/佛/御兽，含单字别名）"""

    def __init__(self, db: DataBase, config: AstrBotConfig, config_manager: ConfigManager, social_mgr=None):
        self.db = db
        self.config = config
        self.config_manager = config_manager
        self.social_mgr_ref = social_mgr  # SocialManager（仙灵加成），main.py 注入
        self.cultivation_manager = CultivationManager(config, config_manager)
        self.pill_manager = PillManager(self.db, self.config_manager)
        self.storage_ring_mgr = StorageRingManager(db, config_manager)
        self.tutorial_mgr = TutorialManager(self.db)
        
        # ===== 修仙界专属节日配置表 =====
        self.FESTIVALS = {
            "01-01": {
                "name": "问道节", "desc": "一元复始，感悟天地法则，闭关修炼效率大增。", 
                "gold": 8888, "exp": 8888, "item": "洗练石", "item_count": 1
            },
            "02-14": {
                "name": "结缘节", "desc": "红线牵缘，道侣情深，天地灵气化作祝福。", 
                "gold": 5200, "exp": 5200, "item": "强化石", "item_count": 2
            },
            "03-03": {
                "name": "蟠桃盛会", "desc": "瑶池金母设宴，三千年一开花，三千年一结果。", 
                "gold": 30000, "exp": 50000, "item": "洗练石", "item_count": 3
            },
            "05-04": {
                "name": "百器争鸣", "desc": "天下炼器师齐聚，地火涌动，正是炼器好时节。", 
                "gold": 10400, "exp": 10400, "item": "混元铁", "item_count": 1
            },
            "06-06": {
                "name": "镇魔日", "desc": "天狗食日，魔气肆虐，天道赐下护身法宝。", 
                "gold": 6666, "exp": 6666, "item": "迷神香", "item_count": 2
            },
            "07-07": {
                "name": "双星会", "desc": "牛郎织女星交汇，此时双修可得双倍天地馈赠。", 
                "gold": 7700, "exp": 7700, "item": "护府令", "item_count": 1
            },
            "08-15": {
                "name": "太阴盛会", "desc": "月圆之夜，太阴之精最盛，适合吸收月华修炼。", 
                "gold": 15000, "exp": 15000, "item": "九叶灵芝", "item_count": 1
            },
            "09-09": {
                "name": "重阳飞升日", "desc": "登高望远，触摸天门，各大宗门发放试炼奖励。", 
                "gold": 9999, "exp": 9999, "item": "洗练石", "item_count": 3
            },
            "10-01": {
                "name": "万仙来朝", "desc": "普天同庆，仙界大开宝库，降下海量修行资源。", 
                "gold": 19490, "exp": 19490, "item": "洗练石", "item_count": 3
            },
            "11-11": {
                "name": "清心寡欲日", "desc": "红尘滚滚，我自独行，极致的孤独带来极致的道心。", 
                "gold": 11110, "exp": 11110, "item": "强化石", "item_count": 5
            },
            "12-22": {
                "name": "阴极冬至", "desc": "阴极之至，阳气始生，天道赐下纯阳之气助你突破。", 
                "gold": 12220, "exp": 12220, "item": "九幽仙芝", "item_count": 1
            },
            "12-25": {
                "name": "瑞雪丰年", "desc": "天降祥瑞，灵石矿脉爆发，万物生机盎然。", 
                "gold": 12250, "exp": 12250, "item": "洗练石", "item_count": 2
            },
        }

    def _avatar_status(self, player: Player) -> str:
        """化身傀儡剩余时间描述"""
        import json as _json
        import time as _time
        try:
            bonus = _json.loads(player.permanent_bonus) if player.permanent_bonus else {}
            until = int(bonus.get("avatar_until", 0))
            if until > int(_time.time()):
                return f"行功中（剩{(until - int(_time.time())) // 86400}天，闭关+20%）"
        except Exception:
            pass
        return "无"

    def _get_today_festival(self) -> dict:
        """获取今天的节日信息（如果没有则返回 None）"""
        today_md = datetime.now().strftime("%m-%d")
        return self.FESTIVALS.get(today_md)

    # ===== 创号：三选一灵根 =====
    def _root_pending_key(self, prefix: str, user_id: str) -> str:
        return f"{prefix}_{user_id}"

    async def _save_root_pending(self, prefix: str, user_id: str, roots, extra=None) -> None:
        import json as _json
        await self.db.ext.set_system_config(
            self._root_pending_key(prefix, user_id),
            _json.dumps({"roots": list(roots), "created_at": int(time.time()), **(extra or {})}, ensure_ascii=False),
        )

    async def _load_root_pending(self, prefix: str, user_id: str):
        import json as _json
        try:
            raw = await self.db.ext.get_system_config(self._root_pending_key(prefix, user_id))
            data = _json.loads(raw) if raw else None
        except Exception:
            data = None
        if not isinstance(data, dict):
            return None
        if not (data.get("roots") or data.get("cultivation_type") or data.get("life_skill")):
            return None
        if int(time.time()) - int(data.get("created_at") or 0) > PENDING_ROOT_TIMEOUT:
            await self._clear_root_pending(prefix, user_id)
            return None
        return data

    async def _clear_root_pending(self, prefix: str, user_id: str) -> None:
        try:
            await self.db.ext.set_system_config(self._root_pending_key(prefix, user_id), "")
        except Exception:
            pass

    def _format_root_choices(
        self, roots, guarantee: str = "", upgraded: bool = False, roll_no: int = 0
    ) -> str:
        lines = ["🎲 你感应到 " + str(len(roots)) + " 条灵根之路：", "━━━━━━━━━━━━━━━"]
        for idx, root in enumerate(roots, start=1):
            name = root.replace("灵根", "")
            speed = self.cultivation_manager._get_root_speed(name)
            desc = self.cultivation_manager._get_root_description(name)
            tag = " ✨升阶" if upgraded and guarantee and name == guarantee else ""
            lines.append(f"{idx}. 【{name}灵根】{desc}（修炼 ×{speed:.2f}）{tag}")
        if guarantee or roll_no:
            lines.append("━━━━━━━━━━━━━━━")
            batch = f"第 {roll_no} 批" if roll_no else "本批"
            if upgraded:
                lines.append(
                    f"✨ 飞升重塑 · {batch}：必出一条比上一世高一档的灵根（已标注）"
                )
            else:
                lines.append(f"🌱 飞升重塑 · {batch}：保底一条与上一世同档的灵根")
            lines.append(f"每 {ROOT_TIER_GUARANTEE_EVERY} 批必升一档")
        lines.append("━━━━━━━━━━━━━━━")
        return "\n".join(lines)

    async def _prev_life_root(self, user_id: str) -> str:
        """上一世飞升时的灵根（道体重塑的保底基准）。"""
        try:
            return (await self.db.ext.get_system_config(prev_root_key(user_id)) or "").strip()
        except Exception:
            return ""

    # ---------- 灵根图鉴 / 说明 ----------

    ELEMENT_ORDER = ("金", "木", "水", "火", "土")
    ELEMENT_ALIAS = {"雷": "木", "风": "木", "冰": "水", "暗": "水", "光": "火"}
    ROOT_BONUS_LABELS = {
        "physical_atk_pct": ("物攻", True),
        "magic_atk_pct": ("法攻", True),
        "hp_pct": ("气血", True),
        "physical_def_pct": ("物防", True),
        "magic_def_pct": ("法防", True),
        "crit_damage": ("暴伤", True),
        "crit_rate": ("暴击", False),
        "lifesteal": ("吸血", False),
        "counter_damage": ("反伤", False),
    }

    def _root_elements_set(self, name: str) -> set:
        try:
            from ..core.element_resonance import ElementResonance

            return set(ElementResonance.root_elements(name))
        except Exception:
            return set()

    def _root_element_text(self, name: str) -> str:
        """灵根对应的基本五行（雷风归木、冰暗归水、光归火）。"""
        elements = self._root_elements_set(name)
        if not elements:
            return "无"
        text = "、".join(e for e in self.ELEMENT_ORDER if e in elements)
        alias = [f"{a}→{m}" for a, m in self.ELEMENT_ALIAS.items() if a in name]
        if alias:
            text += f"（{'、'.join(alias)}）"
        return text

    def _root_element_bonus_text(self, element: str, share: float = 1.0) -> str:
        try:
            from ..core.element_resonance import ROOT_BONUS

            data = ROOT_BONUS.get(element) or {}
        except Exception:
            data = {}
        parts = []
        for key, value in data.items():
            label, is_pct = self.ROOT_BONUS_LABELS.get(key, (key, True))
            try:
                val = float(value) * share
            except (TypeError, ValueError):
                continue
            parts.append(f"{label} +{val:.1%}" if is_pct else f"{label} +{val:g}")
        return " · ".join(parts) if parts else "—"

    def _format_root_atlas(self) -> str:
        cm = self.cultivation_manager
        lines = ["🌱 灵根图鉴（共 10 档 · 由低到高）", "━━━━━━━━━━━━━━━"]
        for idx, (_, label) in enumerate(cm.root_tier_order):
            roots = cm.root_tier_roots(idx)
            speeds = [cm._get_root_speed(r) for r in roots]
            low, high = min(speeds), max(speeds)
            speed_text = f"×{low:.2f}" if high - low < 0.005 else f"×{low:.2f}~{high:.2f}"
            chance = cm.root_draw_chance(roots[0])
            lines.append(f"{idx + 1}. 【{label}】{speed_text} · 单条 {chance:.2%}")
            lines.append(f"   {' / '.join(roots)}")
        lines.append("━━━━━━━━━━━━━━━")
        lines.append("五行归属：金木水火土按字面；雷/风→木 · 冰/暗→水 · 光→火；阴阳/融合/混沌包罗五行（加成按 1/5 分摊）")
        lines.append("💡 发送「灵根说明 雷」查看单条灵根的倍率、五行与共鸣加成")
        return "\n".join(lines)

    def _format_root_detail(self, name: str) -> str:
        cm = self.cultivation_manager
        name = cm.root_name(name)
        idx = cm.root_tier_index(name)
        if idx < 0:
            return (
                f"❌ 没找到灵根【{name}】。\n"
                "可查：伪 / 金木水火（四灵根）/ 金木（双灵根）/ 金（五行单灵根）/ 雷冰风暗光 / 天金~天雷 / 阴阳 融合 / 混沌 / 先天道体 神圣体质\n"
                "💡 发送「灵根图鉴」看全部 10 档"
            )
        roots = cm.root_tier_roots(idx)
        speed = cm._get_root_speed(name)
        chance = cm.root_draw_chance(name)
        elements = [e for e in self.ELEMENT_ORDER if e in self._root_elements_set(name)]
        share = 1.0 / len(elements) if elements else 1.0

        lines = [
            f"🌱 灵根说明 · {name}",
            "━━━━━━━━━━━━━━━",
            f"品阶：{cm.root_tier_label(name)}（第 {idx + 1} 档 / 共 {len(cm.root_tier_order)} 档）",
            f"修炼速度：×{speed:.2f}",
            f"五行归属：{self._root_element_text(name)}",
            f"单抽概率：{chance:.2%}",
            f"说明：{cm._get_root_description(name)}",
            "━━━━━━━━━━━━━━━",
            f"同档灵根：{' / '.join(roots)}",
        ]
        if idx > 0:
            lower = cm.root_tier_roots(idx - 1)
            lower_speed = cm._get_root_speed(lower[0])
            lines.append(f"低一档：{cm.root_tier_label(lower[0])} ×{lower_speed:.2f}（{' / '.join(lower[:6])}{'…' if len(lower) > 6 else ''}）")
        if idx + 1 < len(cm.root_tier_order):
            higher = cm.root_tier_roots(idx + 1)
            higher_speed = cm._get_root_speed(higher[0])
            lines.append(f"高一档：{cm.root_tier_label(higher[0])} ×{higher_speed:.2f}（{' / '.join(higher)}）")
        else:
            lines.append("高一档：已是最高档，无法再升")
        lines.append("━━━━━━━━━━━━━━━")
        if elements:
            title = "五行共鸣（主修同属性时生效）" + ("·多灵根按 1/%d 分摊" % len(elements) if len(elements) > 1 else "")
            lines.append(title)
            for element in elements:
                lines.append(f"  {element}：{self._root_element_bonus_text(element, share)}")
        else:
            lines.append("五行共鸣：无（该灵根未对应任何五行）")
        return "\n".join(lines)

    async def handle_root_info(self, event: AstrMessageEvent, name: str = ""):
        """灵根图鉴 / 说明：查询阶位、修炼倍率、五行归属与共鸣加成。"""
        key = self.cultivation_manager.root_name(name or "")
        if not key:
            yield event.plain_result(self._format_root_atlas())
            return
        yield event.plain_result(self._format_root_detail(key))

    async def handle_start_xiuxian(self, event: AstrMessageEvent, cultivation_type: str = ""):
        """处理创建角色：先选职业，再从 3 条灵根中选 1 条。

        飞升后玩家处于「待重塑」状态（cultivation_type == NEWLIFE_MARKER），
        同样走这条流程，但不新建角色，而是把新道体写回原行。
        """
        user_id = event.get_sender_id()
        existing = await self.db.get_player_by_id(user_id)
        reshaping = bool(existing and existing.cultivation_type == NEWLIFE_MARKER)
        if existing and not reshaping:
            yield event.plain_result("道友，你已踏入仙途，无需重复此举。")
            return

        if not cultivation_type or cultivation_type.strip() == "":
            raw_msg = ""
            try:
                raw_msg = (event.get_message_str() or "").strip()
            except Exception:
                pass
            for alias in sorted(CULTIVATION_ALIASES.keys(), key=len, reverse=True):
                if alias in raw_msg:
                    cultivation_type = alias
                    break
            if not cultivation_type:
                role_lines = "\n".join(
                    f"【{name}】定位：{CLASS_ROLE_INFO[name]['name']}\n"
                    f"  {CLASS_ROLE_INFO[name]['desc']}"
                    for name in VALID_CULTIVATIONS
                )
                header = (
                    "🌀 道体重塑\n"
                    "━━━━━━━━━━━━━━━\n"
                    "飞升已散尽旧道体，请重新选择修炼方式（发送：我要修仙 职业名）：\n\n"
                    if reshaping
                    else "🌟 欢迎踏入修仙之路！\n"
                    "━━━━━━━━━━━━━━━\n"
                    "请选择你的修炼方式（发送：我要修仙 职业名）：\n\n"
                )
                yield event.plain_result(
                    header
                    + f"{role_lines}\n"
                    "━━━━━━━━━━━━━━━\n"
                    f"💡 使用方法：\n  {CMD_START_XIUXIAN} 剑修\n"
                    f"（也支持单字：{CMD_START_XIUXIAN} 剑）"
                )
                return

        cultivation_type = cultivation_type.strip().replace("　", " ").strip()
        normalized = CULTIVATION_ALIASES.get(cultivation_type, cultivation_type)
        if normalized not in VALID_CULTIVATIONS:
            yield event.plain_result(
                f"❌ 职业选择错误！收到的是「{cultivation_type}」。\n"
                f"请从以下 7 种职业中选择其一：灵修 / 体修 / 魔修 / 剑修 / 鬼修 / 佛修 / 御兽\n"
                f"（也支持单字简称：灵 / 体 / 魔 / 剑 / 鬼 / 佛 / 兽）\n"
                f"例如：我要修仙 剑修"
            )
            return

        roots, guarantee, upgraded, roll_no = [], "", False, 0
        extra = {"cultivation_type": normalized}
        note = ""
        if reshaping:
            prev_root = await self._prev_life_root(user_id)
            if await self._root_roll_no(user_id) == 0:
                # 飞升事务已把批次与免费次数清零，重塑的第一批不额外扣次数
                roots, guarantee, upgraded, roll_no = await self._draw_roots_with_guarantee(
                    existing, prev_root
                )
            else:
                # 本轮已经抽过一批：再发「我要修仙」等同于重抽，要扣免费次数/洗炼额度，
                # 否则可以反复重开把批次推到第 5 批白嫖升档
                ok, note, roots, info = await self._reroll_roots(existing, prev_root)
                if not ok:
                    yield event.plain_result(
                        note + f"\n💡 也可以直接发送 `{CMD_CHOOSE_ROOT} 1/2/3` 用上一批结果。"
                    )
                    return
                guarantee = info.get("root", "")
                upgraded = bool(info.get("upgraded"))
                roll_no = int(info.get("roll_no") or 0)
            extra["newlife"] = 1
            extra["guaranteed_root"] = guarantee
            extra["upgraded"] = 1 if upgraded else 0
            extra["roll_no"] = roll_no
        else:
            roots = self.cultivation_manager.draw_spiritual_roots(3)
        await self._save_root_pending("create_roots", user_id, roots, extra)
        budget = ""
        if reshaping:
            budget = "\n" + await self._reroll_budget_text(existing) + " · 发送 `/选择灵根 重抽` 换一批"
            if note:
                budget += f"（{note}）"
        yield event.plain_result(
            self._format_root_choices(roots, guarantee, upgraded, roll_no)
            + f"\n请发送：{CMD_CHOOSE_ROOT} 1/2/3 选择灵根（10 分钟内有效）"
            + budget
        )

    async def handle_choose_root(self, event: AstrMessageEvent, choice: str = ""):
        """选择灵根：创号 / 道体重塑 / 重修重随；支持「重抽」再抽一批。"""
        user_id = event.get_sender_id()
        text = (choice or "").strip().translate(str.maketrans("１２３", "123"))

        # 「重抽」：按当前处于哪条待选流程重抽，免费 5 次 / 商店额度
        if text in ("重抽", "重随", "换一批", "重随灵根", "换灵根"):
            pending_create = await self._load_root_pending("create_roots", user_id)
            player = await self.db.get_player_by_id(user_id)
            if pending_create:
                if not player or pending_create.get("newlife") != 1:
                    yield event.plain_result("❌ 创号阶段不能重抽，请直接选择 1/2/3。")
                    return
                ok, note, roots, info = await self._reroll_roots(
                    player, await self._prev_life_root(user_id)
                )
                if not ok:
                    yield event.plain_result(note)
                    return
                extra = {
                    k: v
                    for k, v in pending_create.items()
                    if k not in ("roots", "created_at")
                }
                extra["guaranteed_root"] = info.get("root", "")
                extra["upgraded"] = 1 if info.get("upgraded") else 0
                extra["roll_no"] = info.get("roll_no", 0)
                await self._save_root_pending("create_roots", user_id, roots, extra)
                yield event.plain_result(
                    self._format_root_choices(
                        roots,
                        info.get("root", ""),
                        bool(info.get("upgraded")),
                        int(info.get("roll_no") or 0),
                    )
                    + f"\n请发送：{CMD_CHOOSE_ROOT} 1/2/3 确定灵根"
                    + f"\n{await self._reroll_budget_text(player)}（{note}）"
                )
                return
            pending_rebirth = await self._load_root_pending("rebirth_options", user_id)
            if pending_rebirth and player:
                async for r in self._reroll_rebirth_roots(player, event, pending_rebirth):
                    yield r
                return
            yield event.plain_result("❌ 当前没有待选择的灵根，请先发送「我要修仙 职业」或「弃道重修 重随灵根」。")
            return

        if not text.isdigit():
            yield event.plain_result(f"❌ 请发送：{CMD_CHOOSE_ROOT} 1/2/3（或「{CMD_CHOOSE_ROOT} 重抽」换一批）")
            return
        index = int(text) - 1

        pending_create = await self._load_root_pending("create_roots", user_id)
        if pending_create:
            roots = pending_create.get("roots") or []
            if not (0 <= index < len(roots)):
                yield event.plain_result(f"❌ 请选择 1~{len(roots)} 之间的数字。")
                return
            cultivation_type = pending_create.get("cultivation_type") or "灵修"
            player = await self.db.get_player_by_id(user_id)
            if player and pending_create.get("newlife") == 1:
                ok, reply = await self._finalize_newlife(player, cultivation_type, roots[index])
                await self._clear_root_pending("create_roots", user_id)
                yield event.plain_result(reply)
                return
            if player:
                await self._clear_root_pending("create_roots", user_id)
                yield event.plain_result("道友，你已踏入仙途，无需重复此举。")
                return
            reply = await self._finalize_create(event, cultivation_type, roots[index])
            await self._clear_root_pending("create_roots", user_id)
            yield event.plain_result(reply)
            return

        pending_rebirth = await self._load_root_pending("rebirth_options", user_id)
        if pending_rebirth:
            player = await self.db.get_player_by_id(user_id)
            if not player:
                await self._clear_root_pending("rebirth_options", user_id)
                yield event.plain_result("❌ 未找到你的角色，无法重修。")
                return
            roots = pending_rebirth.get("roots") or []
            if not (0 <= index < len(roots)):
                yield event.plain_result(f"❌ 请选择 1~{len(roots)} 之间的数字。")
                return
            ok, msg = await self._perform_rebirth(
                player,
                roots[index],
                cultivation_type=str(pending_rebirth.get("cultivation_type") or ""),
                life_skill=str(pending_rebirth.get("life_skill") or ""),
            )
            if not ok:
                yield event.plain_result(msg)
                return
            await self._clear_root_pending("rebirth_options", user_id)
            yield event.plain_result(msg)
            return

        yield event.plain_result("❌ 没有待选择的灵根，请先发送「我要修仙 职业」或「弃道重修 重随灵根」。")

    async def _finalize_newlife(self, player: Player, cultivation_type: str, root: str) -> tuple[bool, str]:
        """飞升后的道体重塑：按所选职业与灵根重建新号身体，其余跨世数据原样保留。"""
        if cultivation_type not in VALID_CULTIVATIONS:
            return False, "❌ 职业选择无效，请重新发送「我要修仙 职业名」。"
        base_player = self.cultivation_manager.generate_new_player_stats(
            player.user_id, cultivation_type, root=root
        )
        player.cultivation_type = base_player.cultivation_type
        player.spiritual_root = base_player.spiritual_root
        player.lifespan = base_player.lifespan
        player.hp = base_player.hp
        player.mp = base_player.mp
        player.atk = base_player.atk
        player.atkpractice = 0
        player.spiritual_qi = base_player.spiritual_qi
        player.max_spiritual_qi = base_player.max_spiritual_qi
        player.blood_qi = base_player.blood_qi
        player.max_blood_qi = base_player.max_blood_qi
        player.magic_damage = base_player.magic_damage
        player.physical_damage = base_player.physical_damage
        player.magic_defense = base_player.magic_defense
        player.physical_defense = base_player.physical_defense
        player.mental_power = base_player.mental_power
        player.level_index = 0
        player.experience = 0
        player.state = "空闲"
        player.cultivation_start_time = 0
        player.tutorial_step = 4
        # 道基淬炼：新身体刚生成，按永久账本重新放大
        apply_dao_gift(player, parse_bonus(player))
        await self.db.update_player(player)

        root_name = player.spiritual_root.replace("灵根", "")
        try:
            root_desc = self.cultivation_manager._get_root_description(root_name)
            root_speed = self.cultivation_manager._get_root_speed(root_name)
        except Exception:
            root_desc, root_speed = "", 0.0
        role_info = CLASS_ROLE_INFO.get(player.cultivation_type, {"name": "未定义", "desc": "无"})
        return True, (
            "🌀 道体重塑完成！\n"
            "━━━━━━━━━━━━━━━\n"
            f"新身份：职业【{player.cultivation_type}】· 灵根【{player.spiritual_root}】\n"
            f"职业定位：{role_info['name']} · {role_info['desc']}\n"
            f"灵根评价：{root_desc}（修炼 ×{float(root_speed):.2f}）\n"
            f"启动灵石：{player.gold:,}\n"
            "生活职业：未选择（发送 /选择生活职业 名字 可立即选择）\n"
            "━━━━━━━━━━━━━━━\n"
            f"💡 发送「{CMD_PLAYER_INFO}」查看状态，重新踏上仙途吧！"
        )

    async def _finalize_create(self, event: AstrMessageEvent, cultivation_type: str, root: str) -> str:
        """按指定灵根创建角色并返回结果文本。"""
        user_id = event.get_sender_id()
        new_player = self.cultivation_manager.generate_new_player_stats(user_id, cultivation_type, root=root)
        try:
            import json as _json
            new_player.learned_recipes = _json.dumps([130, 131], ensure_ascii=False)
        except Exception:
            pass
        await self.db.create_player(new_player)
        try:
            await self.db.ext.ensure_impart_info(user_id)
        except Exception:
            pass
        new_player_title_msg = ""
        try:
            raw_total = await self.db.ext.get_system_config(f"rebirth_total_{user_id}")
            total = int(raw_total or 0)
            if total >= 3:
                from ..managers.title_manager import TitleManager
                tm = TitleManager(self.db)
                new_title, tmsg = tm.grant(new_player, "百变星君")
                if new_title:
                    await self.db.update_player(new_player)
                    new_player_title_msg = tmsg.strip() if tmsg else ""
        except Exception:
            new_player_title_msg = ""

        root_name = new_player.spiritual_root.replace("灵根", "")
        root_description = self.cultivation_manager._get_root_description(root_name)
        role_info = CLASS_ROLE_INFO.get(
            new_player.cultivation_type, {"name": "未定义", "desc": "无"}
        )
        reply_msg = (
            f"🎉 恭喜道友 {event.get_sender_name()} 踏上仙途！\n"
            f"━━━━━━━━━━━━━━━\n"
            f"修炼方式：【{new_player.cultivation_type}】\n"
            f"职业定位：{role_info['name']} · {role_info['desc']}\n"
            f"灵根：【{new_player.spiritual_root}】\n"
            f"评价：{root_description}\n"
            f"启动资金：{new_player.gold} 灵石\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💡 发送「{CMD_PLAYER_INFO}」查看状态"
        )
        tutorial_msg = await self.tutorial_mgr.check_and_advance(new_player, "create")
        reply_msg += tutorial_msg
        if new_player_title_msg:
            reply_msg += f"\n{new_player_title_msg}"
        return reply_msg

    @player_required
    async def handle_player_info(self, player: Player, event: AstrMessageEvent):
        """处理查看玩家信息"""
        display_name = event.get_sender_name()
        required_exp = player.get_required_exp(self.config_manager)

        await self.pill_manager.update_temporary_effects(player)
        pill_multipliers = self.pill_manager.calculate_pill_attribute_effects(player)

        from ..core import EquipmentManager
        equipment_manager = EquipmentManager(self.db, self.config_manager)
        equipped_items = equipment_manager.get_equipped_items(
            player, self.config_manager.items_data, self.config_manager.weapons_data
        )
        total_attrs = player.get_total_attributes(equipped_items, pill_multipliers, equipment_manager.get_base_attributes(player))

        from ..managers.combat_manager import CombatManager
        impart_info = await self.db.ext.get_impart_info(player.user_id)
        effects = equipment_manager.get_combat_effects(player)

        combat_panel = CombatManager.build_combat_panel(
            player, pill_multipliers, impart_info, self.config_manager, effects
        )
        # 名片与实战一致：主战特效 + 全部出战灵兽攻击
        try:
            from ..managers.pet_manager import PetManager
            pet_mgr = PetManager(self.db)
            main_pet, support_pets, pet_effects, owner_atk = await pet_mgr.build_pet_lineup(
                player.user_id,
                combat_panel["hp"],
                combat_panel["atk"],
                combat_panel["defense"],
                player.cultivation_type,
                persist=False,
            )
            if main_pet or support_pets:
                CombatManager.apply_pet_lineup(
                    combat_panel,
                    main_pet,
                    support_pets,
                    pet_effects,
                    owner_atk,
                )
        except Exception:
            pass

        combat_power = CombatManager.calculate_combat_power(
            combat_panel, total_attrs["mental_power"]
        )
        from ..core.element_resonance import ElementResonance
        resonance = ElementResonance.compute(player, self.config_manager)
        element_name = resonance.get("main_element") or "无"
        resonance_desc = ElementResonance.describe(resonance)
        resonance_bonus = ElementResonance.bonus_text(resonance)
        resonance_text = resonance_desc + (f"（{resonance_bonus}）" if resonance_bonus else "")
        # 卡片按行折行，描述与加成拆成两行，避免长括号被拆到单独一行。
        resonance_card_rows = []
        if resonance.get("main_element"):
            resonance_card_rows.append(("五行共鸣", resonance_desc))
            if resonance_bonus:
                resonance_card_rows.append(("共鸣加成", resonance_bonus))
        from ..managers.luck_manager import LuckManager
        luck_parts = LuckManager.get_breakdown(player, self.config_manager)
        luck_total = luck_parts["total"]
        luck_detail = (
            f"本体{luck_parts['base']}"
            + (f" + 称号{luck_parts['title']}" if luck_parts["title"] else "")
            + (f" + 词条{luck_parts['affix']}" if luck_parts["affix"] else "")
            + (f" + 符箓{luck_parts['talisman']}" if luck_parts["talisman"] else "")
            + (f" + 功法{luck_parts['technique']}" if luck_parts.get("technique") else "")
        )
        
        sect_name = "无宗门"
        position_name = "散修"
        if player.sect_id and player.sect_id != 0:
            sect = await self.db.ext.get_sect_by_id(player.sect_id)
            if sect:
                sect_name = sect.sect_name
                if sect.sect_owner == player.user_id: position_name = "宗主"
                elif player.sect_position == 1: position_name = "长老"
                elif player.sect_position == 2: position_name = "亲传弟子"
                elif player.sect_position == 3: position_name = "内门弟子"
                else: position_name = "外门弟子"
        
        from ..core.item_rules import ItemRules
        gear_rows = [(ItemRules.SLOT_LABELS[slot], name or "无")
                     for slot, (name, _) in equipment_manager.get_gear_slots(player).items()]
        gear_text = "".join(f"  {label}：{name}\n" for label, name in gear_rows)
        technique_name = player.main_technique if player.main_technique else "无"
        from ..core.technique_bonus import TechniqueBonusManager
        life = TechniqueBonusManager.collect_life_effects(player, self.config_manager)
        life_text = []
        if life.get("gold_bonus"):
            life_text.append(f"灵石+{life['gold_bonus']:.1%}")
        if life.get("drop_bonus"):
            life_text.append(f"掉落+{life['drop_bonus']:.1%}")
        if life.get("cooldown_reduction"):
            life_text.append(f"冷却-{life['cooldown_reduction']:.1%}")
        if life.get("luck_bonus"):
            life_text.append(f"气运+{int(life['luck_bonus'])}")
        technique_life_text = " · ".join(life_text) if life_text else "无"
        
        dao_hao = player.user_name if player.user_name else display_name
        display_title = f"【{player.title}】" if player.title else ""
        full_name = f"{display_title}{dao_hao}"

        # 状态以 user_cd 为准（闭关/历练/押镖/秘境/宗门任务等都会写这里），
        # player.state 只在闭关时维护，不能作为唯一来源。
        from ..core.avatar_rules import avatar_count, total_slots

        user_cd = await self.db.ext.first_busy_cd(player.user_id)
        busy_count = await self.db.ext.busy_slot_count(player.user_id)
        slot_total = total_slots(player)
        slot_text = (
            f"本体 + {avatar_count(player)} 尊化身（{busy_count}/{slot_total} 出动，"
            f"空闲 {slot_total - busy_count}）"
            if avatar_count(player)
            else f"仅本体（{busy_count}/1 出动）"
        )
        if user_cd:
            status_text = UserStatus.get_name(user_cd.type)
            if slot_total > 1:
                status_text += f" · 分身 {busy_count}/{slot_total} 出动"
        else:
            status_text = player.state or "空闲"

        if (
            combat_panel.get("max_hp", 0) > 0
            and combat_panel.get("hp", 0) / combat_panel["max_hp"] <= 0.20
        ):
            status_text = "受伤（气血不足20%）"

        reply_msg = (
            f"📋 道友 {full_name} 的信息\n"
            f"━━━━━━━━━━━━━━━\n"
            f"【基本信息】\n"
            f"  道号：{full_name}\n"
            f"  境界：{player.get_level(self.config_manager)}\n"
            f"  修为：{int(player.experience):,}/{int(required_exp):,}\n"
            f"  灵石：{player.gold:,}\n"
            f"  战力：{combat_power:,}\n"
            f"  灵根：{player.spiritual_root}\n"
            f"  气运：{luck_total}（{luck_detail}）\n"
            f"【修炼属性】\n"
            f"  修炼方式：{player.cultivation_type}\n"
            f"  状态：{status_text}\n"
            f"  分身：{slot_text}\n"
            f"  寿命：{player.lifespan}\n"
            f"  精神力：{total_attrs['mental_power']}\n"
            f"  基础物伤：{player.physical_damage} | 基础法伤：{player.magic_damage}\n"
            f"  基础物防：{player.physical_defense} | 基础法防：{player.magic_defense}\n"
        )

        if player.cultivation_type == "体修":
            reply_msg += (
                f"  体修气血：{player.blood_qi}/{total_attrs.get('max_blood_qi', 0)}\n"
            )

        reply_msg += (
            f"【战斗属性】\n"
            f"  气血：{combat_panel['hp']:,}/{combat_panel['max_hp']:,}\n"
            f"  灵气：{combat_panel['mp']:,}/{combat_panel['max_mp']:,}\n"
            f"  修为溢出：+{combat_panel.get('overflow_bonus', 0):.1%}\n"
            f"  物攻：{combat_panel.get('physical_atk', 0):,} | 法攻：{combat_panel.get('magic_atk', 0):,}\n"
            f"  物防：{combat_panel.get('physical_defense', 0):,} | 法防：{combat_panel.get('magic_defense', 0):,}\n"
            f"  暴击：{combat_panel['crit_rate']:.1f}% | 暴伤：{combat_panel['crit_damage'] * 100:.1f}%\n"
            f"  吸血：{combat_panel['lifesteal']:.1f}% | 反伤：{combat_panel['counter_damage']:.1f}%\n"
            f"  五行：{element_name}\n"
            f"  五行共鸣：{resonance_text}\n"
        )

        reply_msg += (
            f"【装备与宗门】\n"
            f"  主修心法：{technique_name}\n"
            f"  功法生活：{technique_life_text}\n"
            f"{gear_text}"
            f"  宗门：{sect_name} | 职位：{position_name}\n"
        )
        
        loan = await self.db.ext.get_active_loan(player.user_id)
        loan_card_rows = []
        if loan:
            now = int(time.time())
            remaining_seconds = loan["due_at"] - now
            remaining_days = remaining_seconds // 86400
            remaining_hours = (remaining_seconds % 86400) // 3600
            days_borrowed = max(1, (now - loan["borrowed_at"]) // 86400)
            accrued_interest = int(
                loan["principal"] * loan["interest_rate"] * days_borrowed
            )
            paid_interest = int(loan.get("paid_interest", 0) or 0)
            paid_principal = int(loan.get("paid_principal", 0) or 0)
            interest_due = max(0, accrued_interest - paid_interest)
            principal_due = max(0, int(loan["principal"]) - paid_principal)
            total_due = principal_due + interest_due
            loan_type_name = "突破贷款" if loan["loan_type"] == "breakthrough" else "普通贷款"
            
            if remaining_seconds <= 0: time_str = "⚠️ 已逾期！"
            elif remaining_days <= 0: time_str = f"🔴 {remaining_hours}小时"
            elif remaining_days <= 1: time_str = f"🟠 {remaining_days}天{remaining_hours}小时"
            else: time_str = f"🟡 {remaining_days}天"
            
            identity_label, purchase_blocked = _loan_identity(
                total_due, max(0, remaining_days), player.level_index
            )
            reply_msg += (
                f"\n🏷️ 身份：{identity_label}（欠款 {total_due:,} 灵石）\n"
                f"【贷款信息】💰\n"
                f"  类型：{loan_type_name}\n"
                f"  应还：{total_due:,} 灵石\n"
                f"  剩余：{time_str}\n"
            )
            if purchase_blocked:
                reply_msg += "  🚫 欠款过多，购买类功能已暂停。\n"

            loan_card_rows = [
                ("身份", identity_label),
                ("类型", loan_type_name),
                ("应还", f"{total_due:,} 灵石"),
                ("剩余", time_str),
            ]
            if purchase_blocked:
                loan_card_rows.append(("提示", "🚫 购买类功能已暂停"))

        if getattr(player, "sect_debt", 0) > 0:
            reply_msg += (
                f"\n【宗门欠款】\n"
                f"  欠款：{player.sect_debt:,} 灵石\n"
                f"  有灵石时会自动偿还；欠款未清前不能退出宗门。\n"
            )
        
        reply_msg += "━━━━━━━━━━━━━━━"

        # ===== 图片卡片：成功只发图，失败/关闭降级为纯文本 =====
        try:
            from ..utils.image_generator import render_card_async, card_enabled
            card_path = None
            if card_enabled("CARD_INFO"):
                attribute_rows = []
                if player.cultivation_type == "体修":
                    attribute_rows.append(
                        ("体修气血", f"{player.blood_qi}/{total_attrs.get('max_blood_qi', 0)}")
                    )
                attribute_rows.extend([
                    ("基础物伤/法伤", f"{player.physical_damage}/{player.magic_damage}"),
                    ("基础物防/法防", f"{player.physical_defense}/{player.magic_defense}"),
                    ("精神力", f"{total_attrs['mental_power']}"),
                    ("状态", status_text),
                    ("分身", slot_text),
                    ("寿命", f"{player.lifespan}"),
                ])

                card_path = await render_card_async(
                    "info",
                    full_name,
                    f"ID: {player.user_id} · {player.get_level(self.config_manager)}",
                    sections=[
                        {"name": "基础", "rows": [
                            ("职业", player.cultivation_type),
                            ("境界", player.get_level(self.config_manager)),
                            ("修为", f"{int(player.experience):,}/{int(required_exp):,}"),
                            ("灵石", f"{player.gold:,}"),
                            ("战力", f"{combat_power:,}"),
                            ("灵根", player.spiritual_root),
                            ("功德/业力", f"{getattr(player, 'karma', 0) or 0:+d}"),
                            ("气运", f"{luck_total}"),
                            ("生活职业", getattr(player, "life_skill", "") or "无"),
                            ("五行属性", element_name),
                            *resonance_card_rows,
                            ("化身", self._avatar_status(player)),
                        ]},
                        {"name": "属性", "rows": attribute_rows},
                        {"name": "战斗", "rows": [
                            ("气血", f"{combat_panel['hp']:,}/{combat_panel['max_hp']:,}"),
                            ("灵气", f"{combat_panel['mp']:,}/{combat_panel['max_mp']:,}"),
                            ("修为溢出", f"+{combat_panel.get('overflow_bonus', 0):.1%}"),
                            ("物攻/法攻", f"{combat_panel.get('physical_atk', 0):,}/{combat_panel.get('magic_atk', 0):,}"),
                            ("物防/法防", f"{combat_panel.get('physical_defense', 0):,}/{combat_panel.get('magic_defense', 0):,}"),
                            ("暴击/暴伤", f"{combat_panel['crit_rate']:.1f}%/{combat_panel['crit_damage'] * 100:.1f}%"),
                            ("吸血/反伤", f"{combat_panel['lifesteal']:.1f}%/{combat_panel['counter_damage']:.1f}%"),
                            ("五行", element_name),
                        ]},
                        {"name": "装备与宗门", "rows": [
                            *gear_rows,
                            ("主修心法", technique_name),
                            ("功法生活", technique_life_text),
                            ("宗门", sect_name),
                            ("职位", position_name),
                        ]},
                    ] + ([{"name": "贷款", "rows": loan_card_rows}] if loan_card_rows else []),
                    footer="模拟修仙 · 发送 /修仙帮助 查看全部指令",
                )
            if card_path:
                yield event.image_result(card_path)
            else:
                yield event.plain_result(reply_msg)
        except Exception as e:
            from astrbot.api import logger
            logger.warning(f"我的信息卡片生成失败（已降级为文本）: {e}")
            yield event.plain_result(reply_msg)

    @player_required
    async def handle_start_cultivation(self, player: Player, event: AstrMessageEvent):
        """处理闭关指令"""
        from ..core.avatar_rules import total_slots

        if await self.db.ext.get_active_cd(player.user_id, UserStatus.CULTIVATING) is not None:
            yield event.plain_result("道友已在闭关中，请勿重复进入。")
            return

        # 兼容旧数据/异常中断：玩家状态仍为修炼中但 user_cd 丢失时，恢复闭关而不是覆盖开始时间
        if (
            player.state == "修炼中"
            and player.cultivation_start_time > 0
        ):
            await self.db.ext.start_activity(
                player.user_id, UserStatus.CULTIVATING, 0,
                max_slots=total_slots(player),
            )
            yield event.plain_result(
                f"🧘 检测到未结算的闭关记录，已恢复闭关状态\n"
                f"💡 发送「{CMD_END_CULTIVATION}」结束闭关"
            )
            return

        started, _slot, why = await self.db.ext.start_activity(
            player.user_id, UserStatus.CULTIVATING, 0, max_slots=total_slots(player)
        )
        if not started:
            yield event.plain_result(
                "道友已在闭关中，请勿重复进入。" if why == "already_running"
                else "❌ 分身都出去了，没人手再开一炉闭关。"
            )
            return

        player.state = "修炼中"
        player.cultivation_start_time = int(time.time())
        await self.db.update_player(player)

        yield event.plain_result(f"🧘 道友已进入闭关状态\n💡 发送「{CMD_END_CULTIVATION}」结束闭关")

    @with_player_lock
    async def handle_end_cultivation(self, event: AstrMessageEvent):
        """处理出关指令（不走忙碌状态拦截，保证任何时候都能出关）"""
        set_event_flag(event, "cultivation_success", False)
        player = await self.db.get_player_by_id(event.get_sender_id())
        if not player:
            yield event.plain_result("道友尚未踏入仙途，请发送「我要修仙」开启你的旅程。")
            return
        ok, reply_msg = await self.settle_cultivation(player)
        if ok:
            set_event_flag(event, "cultivation_success", True)
        yield event.plain_result(reply_msg)

    async def settle_cultivation(self, player: Player):
        """结算一次闭关，返回 ``(是否成功结算, 提示文本)``。

        手动 /出关 与历练代理的「CD 自动闭关」共用同一套结算，
        避免自动代理另写一份导致丹药加权、家具/宗门/魔潮加成口径漂移。
        """
        user_cd = await self.db.ext.get_active_cd(player.user_id, UserStatus.CULTIVATING)
        legacy_cultivating = player.state == "修炼中" and user_cd is None
        if user_cd is None and not legacy_cultivating:
            return False, "道友当前并未闭关，无需出关。"
        if player.cultivation_start_time == 0:
            player.state = "空闲"
            player.cultivation_start_time = 0
            await self.db.update_player(player)
            await self.db.ext.set_user_free(player.user_id, cd_type=UserStatus.CULTIVATING)
            return False, "闭关记录异常，已为你恢复空闲状态，请重新闭关。"

        end_time = int(time.time())
        duration_seconds = end_time - player.cultivation_start_time
        duration_minutes = duration_seconds // 60

        if duration_minutes < 1:
            player.state = "空闲"
            player.cultivation_start_time = 0
            await self.db.update_player(player)
            await self.db.ext.set_user_free(player.user_id, cd_type=UserStatus.CULTIVATING)
            return False, "道友闭关时间不足1分钟，已出关，未获得修为。"

        try:
            cultivation_cfg = self.config_manager.game_config.get("cultivation", {})
        except AttributeError:
            cultivation_cfg = {}
        if not isinstance(cultivation_cfg, dict):
            cultivation_cfg = {}
        try:
            base_minutes = int(cultivation_cfg.get("max_cultivation_minutes", 1440))
        except (TypeError, ValueError):
            base_minutes = 1440
        base_minutes = max(1, base_minutes)
        realm_bonus = (player.level_index // 9) * 360
        MAX_CULTIVATION_MINUTES = base_minutes + realm_bonus
        effective_minutes = min(duration_minutes, MAX_CULTIVATION_MINUTES)
        exceeded_time = duration_minutes > MAX_CULTIVATION_MINUTES

        # 闭关期间可能已经过期的丹药/符箓，清理前先留快照：
        # 结算要按「效果到底覆盖了闭关时段的多少分钟」加权，中途过期的也贡献过修为。
        begin_ts = int(player.cultivation_start_time)
        end_ts = begin_ts + effective_minutes * 60
        effects_snapshot = [
            dict(e) for e in player.get_active_pill_effects() if isinstance(e, dict)
        ]

        await self.pill_manager.update_temporary_effects(player)
        pill_multipliers = self.pill_manager.calculate_pill_attribute_effects(player)

        # 修炼加速改为按时段加权，不再看「出关那一瞬间还剩没剩」
        try:
            from ..core.cultivation_manager import weighted_cultivation_multiplier
            cap = 3.0
            try:
                cap = float(
                    self.config_manager.pill_quality_data.get("temporary_effect_cap", 3.0)
                )
            except Exception:
                cap = 3.0
            cult_bonus = weighted_cultivation_multiplier(effects_snapshot, begin_ts, end_ts, cap)
        except Exception:
            cult_bonus = {
                "multiplier": 1.0, "segments": [], "covered_minutes": 0,
                "total_minutes": effective_minutes, "uncovered_minutes": effective_minutes,
            }

        # 永久丹药加成压根不看时段，全额计入
        permanent_cult = 0.0
        try:
            level_gains = player.get_permanent_pill_gains().get(f"level_{player.level_index}") or {}
            if isinstance(level_gains, dict):
                permanent_cult = float(level_gains.get("cultivation_multiplier") or 0)
        except Exception:
            permanent_cult = 0.0

        pill_multipliers = dict(pill_multipliers)
        pill_multipliers["cultivation_speed"] = cult_bonus["multiplier"] + permanent_cult

        technique_bonus = 0.0
        try:
            from ..core import EquipmentManager
            equipment_manager = EquipmentManager(self.db, self.config_manager)
            equipped_items = equipment_manager.get_equipped_items(
                player, self.config_manager.items_data, self.config_manager.weapons_data
            )
            seen_techniques = set()
            for item in equipped_items:
                if (
                    item.item_type in ("main_technique", "technique")
                    and item.name not in seen_techniques
                ):
                    seen_techniques.add(item.name)
                    technique_bonus += item.exp_multiplier
        except Exception:
            technique_bonus = 0.0

        gained_exp = self.cultivation_manager.calculate_cultivation_exp(
            player, effective_minutes, technique_bonus, pill_multipliers
        )

        # 丹符加成明细：既然算进数值了，就得让人在结算里看得见
        pill_note = ""
        try:
            from ..core.cultivation_manager import format_bonus_note
            pill_note = format_bonus_note(cult_bonus, permanent_cult, effects_snapshot)
        except Exception:
            pill_note = ""

        # 聚灵蒲团：洞天家具，闭关收益+5%
        furniture_note = ""
        try:
            land = await self.db.conn.execute(
                "SELECT furniture FROM blessed_lands WHERE user_id = ?", (player.user_id,)
            )
            row = await land.fetchone()
            if row and row[0] and "聚灵蒲团" in row[0]:
                gained_exp = int(gained_exp * 1.05)
                furniture_note = "\n🛋️ 聚灵蒲团生效：闭关收益+5%"
        except Exception:
            pass

        # 宗门藏经阁：全宗闭关收益+3%/级
        try:
            if player.sect_id:
                sect = await self.db.ext.get_sect_by_id(player.sect_id)
                if sect:
                    import json as _json
                    levels = _json.loads(getattr(sect, "buildings", "{}") or "{}")
                    lib_lv = int(levels.get("藏经阁", 0))
                    if lib_lv > 0:
                        gained_exp = int(gained_exp * (1 + 0.03 * lib_lv))
                        furniture_note += f"\n🏛️ 宗门藏经阁 Lv.{lib_lv} 生效：+{3 * lib_lv}%"
        except Exception:
            pass

        # 魔潮天灾：期间及余波中全服修炼效率-20%；抵御魔潮的义士获24小时天道庇佑(+10%)
        tide_note = ""
        try:
            tide_flag = await self.db.ext.get_system_config("tide_active")
            tide_aftermath = await self.db.ext.get_system_config("tide_aftermath_until")
            in_aftermath = bool(tide_aftermath) and int(time.time()) < int(tide_aftermath)
            if tide_flag == "1" or in_aftermath:
                gained_exp = int(gained_exp * 0.8)
                tide_note = "\n🌑 魔气侵蚀：修炼收益-20%"
            bless_until = await self.db.ext.get_system_config(f"tide_bless_{player.user_id}")
            if bless_until and int(time.time()) < int(bless_until):
                gained_exp = int(gained_exp * 1.10)
                tide_note += "\n🌟 天道庇佑：修炼收益+10%"
        except Exception:
            pass

        # 仙灵道侣加成（玄霜：闭关最高+6%）
        try:
            comp_bonus = await self.social_mgr_ref.companion_bonus_async(player.user_id, "cultivate")
            if comp_bonus > 0:
                gained_exp = int(gained_exp * (1 + comp_bonus))
                tide_note += f"\n💞 仙灵相伴：修炼收益+{int(comp_bonus * 100)}%"
        except Exception:
            pass

        # 化身傀儡：激活期间化身代为行功，闭关收益+20%
        try:
            import json as _json2
            _bonus = _json2.loads(player.permanent_bonus) if player.permanent_bonus else {}
            _avatar_until = int(_bonus.get("avatar_until", 0))
            if _avatar_until > int(time.time()):
                gained_exp = int(gained_exp * 1.20)
                tide_note += "\n🤖 化身行功：修炼收益+20%"
        except Exception:
            pass

        try:
            from ..managers.title_manager import TitleManager
            title_exp_bonus = TitleManager.get_exp_bonus(player)
            if title_exp_bonus > 0:
                gained_exp = int(gained_exp * (1 + title_exp_bonus))
                tide_note += f"\n🏅 称号修为加成：+{int(title_exp_bonus * 100)}%"
        except Exception:
            pass

        player.experience += gained_exp
        player.state = "空闲"
        player.cultivation_start_time = 0
        await self.db.update_player(player)
        await self.db.ext.set_user_free(player.user_id, cd_type=UserStatus.CULTIVATING)

        await self.db.ext.add_task_progress(player.user_id, "cultivate", 1)
        tutorial_msg = await self.tutorial_mgr.check_and_advance(player, "cultivate")

        hours = duration_minutes // 60
        minutes = duration_minutes % 60
        time_str = f"{hours}小时{minutes}分钟" if hours > 0 else f"{minutes}分钟"

        exceed_msg = f"\n⚠️ 闭关超过{MAX_CULTIVATION_MINUTES//60}小时，仅计算前{MAX_CULTIVATION_MINUTES//60}小时修为" if exceeded_time else ""
        
        reply_msg = (
            "🌟 道友出关成功！\n"
            "━━━━━━━━━━━━━━━\n"
            f"⏱️ 闭关时长：{time_str}\n"
            f"📈 获得修为：{gained_exp:,}{exceed_msg}{pill_note}{furniture_note}{tide_note}\n"
            f"💫 当前修为：{player.experience:,}\n"
        )
        reply_msg += tutorial_msg
        return True, reply_msg

    @player_required
    async def handle_check_in(self, player: Player, event: AstrMessageEvent):
        """处理签到指令（包含修仙界节日特殊福利）"""
        today = datetime.now().strftime("%Y-%m-%d")
        if player.last_check_in_date == today:
            yield event.plain_result("📅 道友今日已经签到过了，请明日再来。")
            return

        check_in_gold_min = self.config.get("VALUES", {}).get("CHECK_IN_GOLD_MIN", 50)
        check_in_gold_max = self.config.get("VALUES", {}).get("CHECK_IN_GOLD_MAX", 500)
        if check_in_gold_min > check_in_gold_max:
            check_in_gold_min, check_in_gold_max = check_in_gold_max, check_in_gold_min

        check_in_gold = random.randint(check_in_gold_min, check_in_gold_max)
        try:
            from ..managers.pet_manager import PetManager
            pet_mgr = PetManager(self.db)
            gold_bonus = await pet_mgr.get_life_bonus(player.user_id, "gold_bonus")
            if gold_bonus > 0:
                check_in_gold = int(check_in_gold * (1 + gold_bonus))
        except Exception:
            pass
        try:
            from ..core.technique_bonus import TechniqueBonusManager
            check_in_gold = TechniqueBonusManager.apply_gold_bonus(
                player, check_in_gold, self.config_manager
            )
        except Exception:
            pass
        player.gold += check_in_gold
        player.last_check_in_date = today
        
        # ===== 修仙界节日福利判定 =====
        festival = self._get_today_festival()
        festival_msg = ""
        if festival:
            festival_gold = festival["gold"]
            try:
                from ..core.technique_bonus import TechniqueBonusManager
                festival_gold = TechniqueBonusManager.apply_gold_bonus(
                    player, festival_gold, self.config_manager
                )
            except Exception:
                pass
            player.gold += festival_gold
            player.experience += festival["exp"]
            
            item_name = festival.get("item")
            item_count = festival.get("item_count", 1)
            if item_name:
                await self.storage_ring_mgr.grant_item(player, item_name, item_count, silent=True)
            
            festival_msg = (
                f"\n🎊【{festival['name']}】专属福利降临！🎊\n"
                f"━━━━━━━━━━━━━━━\n"
                f"“{festival['desc']}”\n"
                f"额外获得：{festival_gold:,} 灵石、{festival['exp']:,} 修为\n"
            )
            if item_name:
                festival_msg += f"福泽恩赐：【{item_name}】x{item_count}\n"
        # =================================

        await self.db.update_player(player)
        await self.db.ext.add_task_progress(player.user_id, "check_in", 1)
        # 称号：累计签到
        try:
            checkin_title_msg = await self.title_hooks.bump(player, "checkin_count") if getattr(self, "title_hooks", None) else ""
        except Exception:
            checkin_title_msg = ""

        reply_msg = (
            "✅ 签到成功！\n"
            "━━━━━━━━━━━━━━━\n"
            f"💰 获得灵石：{check_in_gold}\n"
            f"💎 当前灵石：{player.gold}\n"
            "━━━━━━━━━━━━━━━\n"
            "明日再来，莫要忘记哦~"
        )
        reply_msg += festival_msg
        title_msg = checkin_title_msg or ""
        if title_msg:
            reply_msg += f"\n{title_msg}"

        # ===== 签到卡片：成功只发图，失败/关闭降级为纯文本（节日自动切换背景）=====
        try:
            from ..utils.image_generator import render_card_async, card_enabled
            card_path = None
            if card_enabled("CARD_CHECKIN"):
                card_path = await render_card_async(
                    "festival" if festival else "login",
                    f"签到成功 · {player.user_name or '道友'}",
                    today,
                    sections=[
                        {"name": "本次收益", "rows": [
                            ("签到灵石", f"+{check_in_gold}"),
                            ("当前灵石", f"{player.gold:,}"),
                            ("当前修为", f"{player.experience:,}"),
                        ] + (
                            [("节日福利", f"+{festival['gold']:,} 灵石 +{festival['exp']:,} 修为")]
                            if festival else []
                        )},
                    ],
                    footer="每日签到 · 节日有大礼 · 明日再来",
                )
            if card_path:
                yield event.image_result(card_path)
            else:
                yield event.plain_result(reply_msg)
        except Exception:
            yield event.plain_result(reply_msg)

    def _rebirth_points(self, level_index: int, experience: int) -> int:
        """重修仙缘点：方案B，约为飞升的 30%~60%。"""
        import math
        return 10 + max(0, int(level_index or 0)) + int(math.log10(max(int(experience or 0), 1)) * 5)

    async def _rebirth_block_reason(self, player: Player):
        if player.cultivation_type == NEWLIFE_MARKER:
            return (
                "🌀 你正处于道体重塑中，请先发送「我要修仙 职业名」选定新职业与灵根，"
                "再考虑重修。"
            )
        user_cd = await self.db.ext.first_busy_cd(player.user_id)
        if user_cd:
            return f"❌ 你当前正在「{UserStatus.get_name(user_cd.type)}」，无法弃道重修。"
        loan = await self.db.ext.get_active_loan(player.user_id)
        if loan:
            return "❌ 你仍有未结清的灵石贷款，无法重修。请先还款。"
        key = f"rebirth_last_{player.user_id}"
        last_ts = await self.db.ext.get_system_config(key)
        if last_ts:
            diff = int(time.time()) - int(last_ts)
            if diff < REBIRTH_COOLDOWN:
                remaining = REBIRTH_COOLDOWN - diff
                days = remaining // 86400
                hours = (remaining % 86400) // 3600
                minutes = (remaining % 3600) // 60
                return f"⌛ 弃道重修冷却中（{days}天{hours}小时{minutes}分钟后可再次重修）"
        return None

    @player_required
    async def handle_rebirth(self, player: Player, event: AstrMessageEvent, confirm_text: str = ""):
        """弃道重修（方案B）：保留仙缘/转生次数/称号/灵根，可免费重选职业与生活职业。"""
        self.last_rebirth_success = False
        block = await self._rebirth_block_reason(player)
        if block:
            yield event.plain_result(block)
            return

        text = (confirm_text or "").strip()
        pending = await self._load_root_pending("rebirth_options", player.user_id) or {}
        current_type = pending.get("cultivation_type") or player.cultivation_type
        from ..managers.life_skill_manager import LIFE_SKILLS
        current_life = pending.get("life_skill") or (getattr(player, "life_skill", "") or "未选择")

        # 支持多种写法：/弃道重修 御兽、/重修 御兽、/弃道重修 职业 御兽、/弃道重修 生活职业 炼丹师
        def _record(field: str, value: str) -> None:
            pending[field] = value

        normalized_text = text.replace(" ", "")
        if text.startswith(("重选职业", "职业")):
            value = text.split("职业", 1)[1].strip() if "职业" in text else ""
            normalized = CULTIVATION_ALIASES.get(value, value)
            if normalized not in VALID_CULTIVATIONS:
                yield event.plain_result(
                    "❌ 职业选择错误，请从：灵修 / 体修 / 魔修 / 剑修 / 鬼修 / 佛修 / 御兽 中选择。"
                )
                return
            _record("cultivation_type", normalized)
            await self._save_root_pending("rebirth_options", player.user_id, pending.get("roots") or [],
                                          {k: v for k, v in pending.items() if k not in ("roots", "created_at")})
            yield event.plain_result(
                f"✅ 已记录重选职业：【{normalized}】\n"
                "再次发送「弃道重修 确认」执行，或继续选择灵根/生活职业。"
            )
            return

        if text.startswith(("生活职业", "生活")):
            value = text.split("生活职业", 1)[1].strip() if "生活职业" in text else text.split("生活", 1)[1].strip()
            if value not in LIFE_SKILLS:
                listing = " / ".join(LIFE_SKILLS.keys())
                yield event.plain_result(f"❌ 生活职业只有：{listing}")
                return
            _record("life_skill", value)
            await self._save_root_pending("rebirth_options", player.user_id, pending.get("roots") or [],
                                          {k: v for k, v in pending.items() if k not in ("roots", "created_at")})
            yield event.plain_result(
                f"✅ 已记录重选生活职业：【{value}】（重修后免费生效）\n"
                "再次发送「弃道重修 确认」执行。"
            )
            return

        if normalized_text in ("重随灵根", "换灵根", "重抽灵根"):
            text = "重随灵根"

        # 直接输入职业/生活职业名也能识别
        if text not in ("重随灵根", "确认", "确定", "执行"):
            normalized = CULTIVATION_ALIASES.get(text, text)
            if normalized in VALID_CULTIVATIONS:
                _record("cultivation_type", normalized)
                await self._save_root_pending("rebirth_options", player.user_id, pending.get("roots") or [],
                                              {k: v for k, v in pending.items() if k not in ("roots", "created_at")})
                yield event.plain_result(
                    f"✅ 已记录重选职业：【{normalized}】\n"
                    "再次发送「弃道重修 确认」执行。"
                )
                return
            if text in LIFE_SKILLS:
                _record("life_skill", text)
                await self._save_root_pending("rebirth_options", player.user_id, pending.get("roots") or [],
                                              {k: v for k, v in pending.items() if k not in ("roots", "created_at")})
                yield event.plain_result(
                    f"✅ 已记录重选生活职业：【{text}】\n"
                    "再次发送「弃道重修 确认」执行。"
                )
                return

        if text in ("确认", "确定", "执行"):
            text = "确认"
        elif text != "重随灵根":
            text = "__help__"

        if text == "重随灵根":
            async for r in self._reroll_rebirth_roots(player, event, pending):
                yield r
            return

        if text == "__help__":
            yield event.plain_result(
                "⚠️ 弃道重修将重置境界、修为、灵石、装备、宗门、洞府、灵田、灵兽等\n"
                "保留：仙缘点、完美转生次数、重修次数、称号、灵根、仙缘商店永久加成\n"
                "注意：重修不计入完美转生次数（只有 /飞升 才算）\n"
                f"当前：职业【{current_type}】· 生活职业【{current_life}】· 灵根【{player.spiritual_root}】\n"
                "━━━━━━━━━━━━━━━\n"
                "可选操作（也支持 /重修 简写）：\n"
                "  /弃道重修 御兽                （直接重选职业）\n"
                "  /弃道重修 炼丹师              （直接重选生活职业）\n"
                "  /弃道重修 职业 体修            （指定职业）\n"
                "  /弃道重修 生活职业 炼丹师      （指定生活职业）\n"
                f"  /弃道重修 重随灵根            （三选一换灵根，本轮免费 {FREE_ROOT_REROLLS} 次，用完消耗商店洗炼额度）\n"
                "  /弃道重修 确认                （按当前选择执行）\n"
                "职业：灵修/体修/魔修/剑修/鬼修/佛修/御兽\n"
                "生活职业：炼器师/符箓师/阵法师/炼丹师\n"
                "限制：3 天一次，需空闲且无贷款；职业与生活职业重选免费；不提供传家宝封印。"
            )
            return

        ok, msg = await self._perform_rebirth(
            player,
            player.spiritual_root.replace("灵根", ""),
            cultivation_type=str(pending.get("cultivation_type") or ""),
            life_skill=str(pending.get("life_skill") or ""),
        )
        if not ok:
            yield event.plain_result(msg)
            return
        await self._clear_root_pending("rebirth_options", player.user_id)
        yield event.plain_result(msg)

    # ===== 灵根重抽预算：每轮 5 次免费 + 商店额度 =====

    def _reroll_key(self, user_id: str) -> str:
        return ROOT_REROLL_KEY.format(uid=user_id)

    def _roll_key(self, user_id: str) -> str:
        return root_roll_key(user_id)

    async def _root_reroll_used(self, user_id: str) -> int:
        try:
            raw = await self.db.ext.get_system_config(self._reroll_key(user_id))
            return max(0, int(raw or 0))
        except (TypeError, ValueError):
            return 0

    async def _reset_root_reroll_round(self, user_id: str) -> None:
        """把免费次数与批次计数归零（飞升 / 重修事务内也会直接写 0）。"""
        for key in (self._reroll_key(user_id), self._roll_key(user_id)):
            try:
                await self.db.ext.set_system_config(key, "0")
            except Exception:
                pass

    async def _root_roll_no(self, user_id: str) -> int:
        try:
            return max(0, int(await self.db.ext.get_system_config(self._roll_key(user_id)) or 0))
        except (TypeError, ValueError):
            return 0

    async def _bump_root_roll_no(self, user_id: str) -> int:
        """本轮第几批灵根（首次抽取算第 1 批）。"""
        no = await self._root_roll_no(user_id) + 1
        try:
            await self.db.ext.set_system_config(self._roll_key(user_id), str(no))
        except Exception:
            pass
        return no

    async def _draw_roots_with_guarantee(
        self, player: Player, prev_root: str = ""
    ) -> tuple[list, str, bool, int]:
        """抽一批 3 条灵根，按「本轮第几批」决定保底强度。

        - 第 5/10/15… 批：保「比上一世高一档」
        - 其余批次：保「与上一世同档」（至少一条灵根不低于上一世档位）
        - 没有上一世记录（首世 / 弃道重修）：纯随机
        返回 (roots, 保底灵根名, 是否升档批, 批次号)。
        """
        cm = self.cultivation_manager
        roll_no = await self._bump_root_roll_no(player.user_id)
        prev_idx = cm.root_tier_index(prev_root) if prev_root else -1
        if prev_idx < 0:
            return cm.draw_spiritual_roots(3), "", False, roll_no

        # 【灵根天眷】永久抬高保底档位；顶档封顶，不会越界
        top = len(cm.root_tier_order) - 1
        base_tier = min(prev_idx + int(permanent_effect(player, "root_tier_bonus")), top)
        upgraded = False
        min_tier = base_tier
        if roll_no % ROOT_TIER_GUARANTEE_EVERY == 0 and base_tier + 1 <= top:
            min_tier = base_tier + 1
            upgraded = True

        roots = cm.draw_spiritual_roots(3, min_tier)
        guarantee = next(
            (r for r in roots if cm.root_tier_index(r) >= min_tier), ""
        )
        return roots, guarantee, upgraded, roll_no

    async def _reroll_budget_text(self, player: Player) -> str:
        used = await self._root_reroll_used(player.user_id)
        total = free_root_rerolls(player)
        free_left = max(0, total - used)
        return format_reroll_budget(free_left, root_reroll_stock(player), total)

    async def _reroll_roots(self, player: Player, prev_root: str = "") -> tuple[bool, str, list, dict]:
        """扣一次重抽预算并抽 3 条新灵根。

        免费 5 次优先，用完后消耗商店【灵根洗炼】额度；两者都用完则拒绝。
        `prev_root` 非空时（飞升重塑），按批次给「同档 / 升档」保底。
        """
        import json as _json

        used = await self._root_reroll_used(player.user_id)
        total_free = free_root_rerolls(player)
        free_left = max(0, total_free - used)
        bonus = parse_bonus(player)
        stock = int(ledger_value(bonus, "root_reroll_stock"))

        if free_left <= 0 and stock <= 0:
            return False, (
                "❌ 本轮灵根重抽次数已用完（免费 5 次 + 洗炼额度 0 次）。\n"
                "💡 可在 `/仙缘商店` 购买【灵根洗炼】额度，或直接确定当前灵根。"
            ), [], {}

        if free_left > 0:
            used += 1
            try:
                await self.db.ext.set_system_config(self._reroll_key(player.user_id), str(used))
            except Exception:
                pass
            note = f"本次用免费重抽，剩 {total_free - used} 次"
        else:
            left = consume_root_reroll(bonus)
            player.permanent_bonus = _json.dumps(bonus, ensure_ascii=False)
            await self.db.update_player(player)
            note = f"本次消耗 1 次洗炼额度，剩 {left} 次"

        roots, guarantee, upgraded, roll_no = await self._draw_roots_with_guarantee(
            player, prev_root
        )
        if guarantee:
            note += f" · 第 {roll_no} 批" + ("升档保底" if upgraded else "同档保底")
        elif prev_root:
            note += f" · 第 {roll_no} 批"
        return True, note, roots, {
            "root": guarantee,
            "upgraded": upgraded,
            "roll_no": roll_no,
        }

    async def _reroll_rebirth_roots(self, player: Player, event: AstrMessageEvent, pending: dict = None):
        """重随灵根（弃道重修）：免费 5 次，用完消耗洗炼额度；保留已选职业/生活职业。"""
        pending = pending or {}
        ok, note, roots, _info = await self._reroll_roots(player)
        if not ok:
            yield event.plain_result(note)
            return
        # 重随灵根走正常抽取（同档/升档保底只属于飞升重塑），清掉可能残留的标记
        pending.pop("guaranteed_root", None)
        pending.pop("upgraded", None)
        pending.pop("roll_no", None)
        extra = {k: v for k, v in pending.items() if k not in ("roots", "created_at")}
        extra["cultivation_type"] = extra.get("cultivation_type") or player.cultivation_type
        extra["life_skill"] = str(extra.get("life_skill") or getattr(player, "life_skill", "") or "")
        await self._save_root_pending("rebirth_options", player.user_id, roots, extra)
        yield event.plain_result(
            self._format_root_choices(roots)
            + f"\n请发送：{CMD_CHOOSE_ROOT} 1/2/3 选择灵根并执行重修"
            + f"\n当前已选：职业【{extra['cultivation_type']}】· 生活职业【{extra['life_skill'] or '未选择'}】"
            + "\n" + await self._reroll_budget_text(player) + f"（{note}）"
        )

    async def _perform_rebirth(self, player: Player, root: str, cultivation_type: str = "", life_skill: str = ""):
        """执行重修重置：保留仙缘/转生次数/称号/灵根；可选免费换职业/生活职业。"""
        block = await self._rebirth_block_reason(player)
        if block:
            return False, block

        target_type = cultivation_type if cultivation_type in VALID_CULTIVATIONS else player.cultivation_type
        points = self._rebirth_points(player.level_index, player.experience)
        peak_level = player.level_index
        base_player = self.cultivation_manager.generate_new_player_stats(
            player.user_id, target_type, root=root
        )
        base_player.spiritual_root = f"{root}灵根"

        bonus_data = parse_bonus(player)
        prev_gold = int(getattr(player, "gold", 0) or 0)
        player.cultivation_type = base_player.cultivation_type
        player.level_index = 0
        player.experience = 0
        # 转世资粮：按上一世巅峰境界的镖银趟数发放，层数由仙缘商店决定
        # 【前世余荫】再按比例保留上一世灵石（最高 90%）
        startup = int(escort_gold_base(peak_level) * ledger_value(bonus_data, "newlife_trips"))
        player.gold = 1000 + startup + int(prev_gold * keep_gold_ratio(bonus_data))
        player.lifespan = base_player.lifespan
        player.hp = base_player.hp
        player.mp = base_player.mp
        player.atk = base_player.atk
        player.atkpractice = 0
        player.spiritual_qi = base_player.spiritual_qi
        player.max_spiritual_qi = base_player.max_spiritual_qi
        player.blood_qi = base_player.blood_qi
        player.max_blood_qi = base_player.max_blood_qi
        player.magic_damage = base_player.magic_damage
        player.physical_damage = base_player.physical_damage
        player.magic_defense = base_player.magic_defense
        player.physical_defense = base_player.physical_defense
        player.mental_power = base_player.mental_power
        player.spiritual_root = base_player.spiritual_root

        # 仙缘商店的【道基淬炼】跨世保留：基础属性刚被覆盖，按永久账本重新放大
        apply_dao_gift(player, bonus_data)
        # 【宿慧觉醒】开局直接顶境界（只给等级与成长，不送修为）
        try:
            apply_start_level(
                player,
                bonus_data,
                peak_level,
                self.config_manager.get_level_data(player.cultivation_type or "灵修"),
            )
        except Exception:
            pass

        player.weapon = ""
        player.armor = ""
        player.main_technique = ""
        player.set_techniques_list([])
        player.set_storage_ring_items({})
        player.set_overflow_items({})
        player.set_equipment_instances([])
        player.set_equipped_instances({})
        player.set_pills_inventory({})
        player.active_pill_effects = "[]"
        player.permanent_pill_gains = "{}"
        player.has_resurrection_pill = False
        player.resurrection_pill_quality = "一般"
        player.has_debuff_shield = False
        player.daily_pill_usage = "{}"

        if life_skill:
            player.life_skill = life_skill
        elif keeps_life_skill(bonus_data) and not player.life_skill:
            # 【一脉相承】没主动换生活职业时，保留上一世的那一份
            player.life_skill = str(getattr(base_player, "life_skill", "") or "") or player.life_skill
        # 重修后生活职业计费清零（下次更换从基础费用开始）
        player.life_skill_changes = 0
        player.life_skill_cost_peak = 0
        player.life_skill_last_change_at = 0

        player.tutorial_step = 4
        # 弃道重修不再计入「完美转生次数」：完美转生只认飞升（见 /飞升）。
        # 重修次数单独落在 system_config 的 rebirth_total_{uid}，与称号【百变星君】同源。
        player.fate_points += points
        player.state = "空闲"
        player.cultivation_start_time = 0

        now = int(time.time())
        try:
            raw_total = await self.db.ext.get_system_config(f"rebirth_total_{player.user_id}")
            total = int(raw_total or 0) + 1
        except (TypeError, ValueError):
            total = 1
        old_sect_id = int(getattr(player, "sect_id", 0) or 0)
        sect_note = "已了结宗门因果"
        player.sect_id = 0
        player.sect_position = 4
        player.sect_contribution = 0
        player.sect_task = 0
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            await self.db.update_player(player, commit=False)
            if old_sect_id:
                sect = await self.db.ext.get_sect_by_id(old_sect_id)
                if sect and sect.sect_owner == player.user_id:
                    members = await self.db.ext.get_sect_members(old_sect_id)
                    successor = next((m for m in members if m.user_id != player.user_id), None)
                    if successor:
                        await self.db.conn.execute(
                            "UPDATE sects SET sect_owner = ? WHERE sect_id = ?",
                            (successor.user_id, old_sect_id),
                        )
                        await self.db.conn.execute(
                            "UPDATE players SET sect_position = 0 WHERE user_id = ?",
                            (successor.user_id,),
                        )
                        sect_note = f"宗主已传位给【{successor.user_name or '弟子'}】"
                    else:
                        await self.db.ext.delete_sect(old_sect_id)
                        sect_note = "宗门无人继承，已解散"
                else:
                    sect_note = "已退出原宗门"
            await self.db.conn.execute("DELETE FROM player_pets WHERE user_id = ?", (player.user_id,))
            if self.db.ext:
                await self.db.ext.return_gifts_for_receiver(player.user_id, commit=False)
            await self.db.conn.execute("DELETE FROM pending_gifts WHERE sender_id = ?", (player.user_id,))
            await self.db.conn.execute("DELETE FROM auction_npc_offers WHERE seller_id = ?", (player.user_id,))
            await self.db.conn.execute("DELETE FROM auction_items WHERE seller_id = ?", (player.user_id,))
            await self.db.conn.execute("DELETE FROM bank_accounts WHERE user_id = ?", (player.user_id,))
            await self.db.conn.execute("DELETE FROM auto_proxy WHERE user_id = ?", (player.user_id,))
            await self.db.conn.execute("DELETE FROM blessed_lands WHERE user_id = ?", (player.user_id,))
            await self.db.conn.execute("DELETE FROM spirit_farms WHERE user_id = ?", (player.user_id,))
            await self.db.conn.execute(
                "UPDATE spirit_eyes SET owner_id = NULL, owner_name = NULL, claim_time = NULL, last_collect_time = NULL WHERE owner_id = ?",
                (player.user_id,),
            )
            await self.db.conn.execute("DELETE FROM npc_partners WHERE user_id = ?", (player.user_id,))
            await self.db.conn.execute(
                "DELETE FROM master_relations WHERE master_id = ? OR disciple_id = ?",
                (player.user_id, player.user_id),
            )
            for table in (
                "bounty_tasks", "daily_tasks", "combat_cooldowns", "combat_daily",
                "world_boss_damage", "world_boss_raid_damage",
                "dual_cultivation", "dual_cultivation_requests", "spar_requests", "sect_tournament_entries",
            ):
                if table in ("dual_cultivation_requests", "spar_requests"):
                    await self.db.conn.execute(
                        f"DELETE FROM {table} WHERE from_id = ? OR target_id = ?",
                        (player.user_id, player.user_id),
                    )
                else:
                    await self.db.conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (player.user_id,))
            await self.db.conn.execute(
                "UPDATE sect_orders SET status = 'cancelled' WHERE status = 'active' AND (issuer_id = ? OR target_id = ?)",
                (player.user_id, player.user_id),
            )
            # 冷却/累计次数/灵根刷新计数与批次同事务落库：并发 /选择灵根 无法绕过冷却检查
            for cfg_key, cfg_val in (
                (f"rebirth_last_{player.user_id}", str(now)),
                (f"rebirth_total_{player.user_id}", str(total)),
                (f"rebirth_root_reroll_{player.user_id}", "0"),
                (self._roll_key(player.user_id), "0"),
            ):
                await self.db.conn.execute(
                    "INSERT INTO system_config (key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = ?",
                    (cfg_key, cfg_val, now, cfg_val, now),
                )
            await self.db.ext.set_user_free(player.user_id, commit=False)
            await self.db.conn.commit()
        except Exception as e:
            await self.db.conn.rollback()
            return False, f"❌ 重修结算失败，已回滚，请稍后再试。（{e}）"

        self.last_rebirth_success = True

        # 称号【百变星君】：累计重修满 3 次即时授予（原先只在建号时检查，重修玩家永远漏发）
        title_msg = ""
        if total >= 3:
            try:
                from ..managers.title_manager import TitleManager
                tm = TitleManager(self.db)
                new_title, tmsg = tm.grant(player, "百变星君")
                if new_title:
                    await self.db.update_player(player)
                    title_msg = (tmsg or "").strip()
            except Exception:
                title_msg = ""

        life_line = f"、生活职业【{player.life_skill}】" if getattr(player, "life_skill", "") else ""
        return True, (
            "💀 弃道重修完成！\n"
            "━━━━━━━━━━━━━━━\n"
            f"保留：仙缘点 +{points}（当前 {player.fate_points}）、"
            f"完美转生 {player.reincarnation_count} 次 · 重修 {total} 次、称号、灵根【{player.spiritual_root}】\n"
            "保留：仙缘商店永久加成（修炼加速 / 道基淬炼 / 各项心得）\n"
            f"新身份：职业【{player.cultivation_type}】{life_line}\n"
            + (f"启动灵石：{startup:,}（转世资粮）\n" if startup else "")
            + "重置：境界、修为、灵石、装备、储物戒、丹药、功法\n"
            f"重置：宗门（{sect_note}）、洞府、灵田、灵兽、挂单、银行存款\n"
            "━━━━━━━━━━━━━━━\n"
            "重修不计入完美转生次数（只有飞升才算）· 3 天内不可再次重修。"
            + (f"\n{title_msg}" if title_msg else "")
        )
