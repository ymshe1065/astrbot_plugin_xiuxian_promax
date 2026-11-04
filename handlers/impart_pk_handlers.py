# handlers/impart_pk_handlers.py
"""传承PK处理器"""
import re
import time
from astrbot.api.event import AstrMessageEvent
from astrbot.api.all import At
from ..data import DataBase
from ..managers.impart_pk_manager import ImpartPkManager
from ..models import Player
from .utils import player_required

__all__ = ["ImpartPkHandlers"]

IMPART_CHALLENGE_COOLDOWN = 300
# 防无限刷：被挑战方冷却（秒）与同一对玩家每日挑战上限
IMPART_DEFENDER_COOLDOWN = 300
IMPART_PAIR_DAILY_LIMIT = 3


class ImpartPkHandlers:
    """传承PK处理器"""
    
    def __init__(self, db: DataBase, impart_pk_mgr: ImpartPkManager):
        self.db = db
        self.impart_pk_mgr = impart_pk_mgr
    
    @player_required
    async def handle_impart_challenge(self, player: Player, event: AstrMessageEvent, target_info: str = ""):
        """发起传承挑战"""
        # 解析目标：优先读取 At 组件，兼容纯数字 ID
        target_id = None
        if hasattr(event, "message_obj") and event.message_obj:
            for comp in event.message_obj.message:
                if isinstance(comp, At):
                    target_id = str(getattr(comp, "qq", "") or getattr(comp, "target", ""))
                    break
        target_id = target_id or self._extract_user_id(target_info)
        if not target_id:
            yield event.plain_result(
                "⚔️ 传承挑战\n"
                "━━━━━━━━━━━━━━━\n"
                "争夺对方的传承加成！\n"
                "胜利：获得传承ATK加成\n"
                "失败：损失1%修为\n"
                "━━━━━━━━━━━━━━━\n"
                "💡 用法：/传承挑战 @某人"
            )
            return
        
        if target_id == player.user_id:
            yield event.plain_result("❌ 不能挑战自己。")
            return

        cd_key = f"impart_pk_cd_{player.user_id}"
        cd_raw = await self.db.ext.get_system_config(cd_key)
        now = int(time.time())
        if cd_raw and cd_raw.isdigit() and now < int(cd_raw):
            remain = int(cd_raw) - now
            yield event.plain_result(
                f"❌ 传承挑战调息中，请 {remain // 60} 分 {remain % 60} 秒后再来。"
            )
            return

        # 获取目标玩家
        target = await self.db.get_player_by_id(target_id)
        if not target:
            yield event.plain_result("❌ 对方还未踏入修仙之路。")
            return
        
        # 防无限刷①：被挑战方冷却（300秒），禁止短时间内反复针对同一目标
        def_cd_key = f"impart_pk_def_cd_{target_id}"
        def_cd_raw = await self.db.ext.get_system_config(def_cd_key)
        if def_cd_raw and def_cd_raw.isdigit() and now < int(def_cd_raw):
            remain = int(def_cd_raw) - now
            yield event.plain_result(
                f"❌ 对方刚被挑战过，请 {remain // 60} 分 {remain % 60} 秒后再来。"
            )
            return

        # 防无限刷②：同一 挑战者→防守者 组合每日挑战上限（3 次）
        pair_key = f"impart_pk_pair_{time.strftime('%Y-%m-%d')}_{player.user_id}_{target_id}"
        pair_raw = await self.db.ext.get_system_config(pair_key)
        pair_count = int(pair_raw) if (pair_raw and pair_raw.isdigit()) else 0
        if pair_count >= IMPART_PAIR_DAILY_LIMIT:
            yield event.plain_result(
                f"❌ 今日对该道侣的挑战次数已达上限（{IMPART_PAIR_DAILY_LIMIT} 次），请明日再来。"
            )
            return

        # 发起挑战
        wins, log, rewards = await self.impart_pk_mgr.challenge_impart(player, target)
        after_now = int(time.time())
        await self.db.ext.set_system_config(
            cd_key, str(after_now + IMPART_CHALLENGE_COOLDOWN)
        )
        # 挑战结束后写入被挑战方冷却与同对每日计数
        await self.db.ext.set_system_config(
            def_cd_key, str(after_now + IMPART_DEFENDER_COOLDOWN)
        )
        await self.db.ext.set_system_config(pair_key, str(pair_count + 1))
        
        if wins:
            result_msg = (
                f"🎉 传承挑战胜利！\n"
                f"━━━━━━━━━━━━━━━\n"
                f"对手：{target.user_name or target_id[:8]}\n"
                f"获得ATK传承：+{rewards.get('impart_atk_gain', 0):.2%}\n"
            )
        else:
            result_msg = (
                f"💀 传承挑战失败...\n"
                f"━━━━━━━━━━━━━━━\n"
                f"对手：{target.user_name or target_id[:8]}\n"
                f"损失修为：-{rewards.get('exp_loss', 0):,}\n"
            )
        
        # 战报展示：challenge_impart 返回末尾数行战报，拼进结果消息，避免接收后被丢弃
        if log:
            result_msg += f"━━━━━━━━━━━━━━━\n📜 战报：\n{log}\n"

        yield event.plain_result(result_msg)
    
    @player_required
    async def handle_impart_ranking(self, player: Player, event: AstrMessageEvent):
        """传承排行榜"""
        rankings = await self.impart_pk_mgr.get_impart_ranking(10)
        
        if not rankings:
            yield event.plain_result("📊 传承排行榜暂无数据。")
            return
        
        lines = ["🏆 传承排行榜\n━━━━━━━━━━━━━━━"]
        for i, r in enumerate(rankings, 1):
            lines.append(f"{i}. {r['user_name']} - ATK+{r['atk_per']:.1%}")
        lines.append("━━━━━━━━━━━━━━━")
        
        yield event.plain_result("\n".join(lines))
    
    def _extract_user_id(self, msg: str) -> str:
        """从消息中提取用户ID"""
        if not msg:
            return ""
        # 匹配 @xxx（CQ 码）
        at_match = re.search(r'\[CQ:at,qq=(\d+)\]', msg)
        if at_match:
            return at_match.group(1)
        # 仅当参数整体为纯数字时才作为ID（fullmatch），避免命中消息中的任意长数字
        cleaned = msg.strip().lstrip("@").strip()
        if re.fullmatch(r'\d{5,12}', cleaned):
            return cleaned
        return ""
