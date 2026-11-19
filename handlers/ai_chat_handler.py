# handlers/ai_chat_handler.py

"""AI 向导指令入口：``/chat``、``/chatall``、``/新手指南``。

- ``/chat`` 只带**本人**上下文，适合问自己的账号情况；
- ``/chatall`` 带**当前频道**近期所有人发言（标注用户名），适合接着群里的话题问；
- ``/新手指南`` 输出人机共用的那份《新手引导》，不消耗模型额度。

回复在当前频道所有人可见，所以玩家快照刻意不含灵石与资产。
"""

from astrbot.api.event import AstrMessageEvent

try:
    from astrbot.api import logger
except ImportError:
    import logging

    logger = logging.getLogger("xiuxian.ai_chat_handler")

from ..managers.ai_chat_manager import MODE_CHANNEL, MODE_PERSONAL
from ..utils.newbie_guide import ensure_newbie_page, newbie_text
from .utils import resolve_greedy_args

__all__ = ["AiChatHandler"]

CHAT_USAGE = (
    "🤖 AI 向导用法\n"
    "━━━━━━━━━━━━━━━\n"
    "/chat <问题>        问玩法、指令、机制（只看你自己的上下文）\n"
    "/chatall <问题>     带上当前频道近期聊天记录一起问\n"
    "/chat 深度 <问题>   让我查得更细（每天限次，回答更完整）\n"
    "/chat 清空          忘掉我这边的对话上下文\n"
    "/新手指南           第一天上手流程（不消耗问答次数）\n"
    "━━━━━━━━━━━━━━━\n"
    "💡 例：/chat 突破失败会掉境界吗\n"
    "💡 例：/chatall 刚才那个秘境掉什么\n"
    "回答只依据本游戏的玩家文档，查不到会直说，不会编数值。"
)

CLEAR_WORDS = frozenset({"清空", "清除", "重置", "忘记", "clear", "reset"})
HELP_WORDS = frozenset({"帮助", "用法", "help", "?" , "？"})
DEEP_WORDS = frozenset({"深度", "deep", "详细查", "深挖"})


class AiChatHandler:
    """把指令参数翻译成一次 ``AIChatManager.ask``，并把结果发回当前频道。"""

    def __init__(self, db, config: dict = None, ai_chat_mgr=None, ctx_mgr=None, admin_checker=None):
        self.db = db
        self.config = config or {}
        self.ai_chat_mgr = ai_chat_mgr
        self.ctx_mgr = ctx_mgr
        self.admin_checker = admin_checker

    # ---------- 上下文坐标 ----------

    @staticmethod
    def origin(event: AstrMessageEvent) -> tuple:
        """返回 (umo, uid, 昵称, 频道)。网页端一个房间有多个仙界频道，必须分开计。"""
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        uid = str(event.get_sender_id() or "")
        try:
            name = str(event.get_sender_name() or "")
        except Exception:
            name = ""
        channel = str(getattr(event, "reply_channel", "") or "")
        return umo, uid, name, channel

    @staticmethod
    def is_private_reply(event: AstrMessageEvent) -> bool:
        """网页端「仅自己 / 看板」回复：只能进个人缓冲，绝不能进频道缓冲。"""
        if getattr(event, "private", False) or getattr(event, "panel", False):
            return True
        return str(getattr(event, "audience", "") or "") == "self"

    def _ai_config(self) -> dict:
        cfg = self.config.get("AI_CHAT", {}) if isinstance(self.config, dict) else {}
        return cfg if isinstance(cfg, dict) else {}

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        if self.admin_checker is None:
            return False
        try:
            return bool(self.admin_checker(event))
        except Exception:
            return False

    # ---------- /chat · /chatall ----------

    async def handle_chat(
        self,
        event: AstrMessageEvent,
        args: str = "",
        channel_mode: bool = False,
        aliases: tuple = (),
    ):
        """``/chat``（个人）与 ``/chatall``（频道）共用一条实现。"""
        mgr = self.ai_chat_mgr
        if mgr is None or not mgr.enabled():
            yield event.plain_result("🤖 AI 向导当前未启用（插件配置 AI_CHAT.ENABLED）。")
            return
        if channel_mode and not mgr.chatall_enabled():
            yield event.plain_result("🤖 频道问答当前未启用（插件配置 AI_CHAT.CHATALL_ENABLED）。")
            return

        umo, uid, name, channel = self.origin(event)
        cmd_name = "chatall" if channel_mode else "chat"
        # GreedyStr 在部分平台会退化成只给第一个分段，用原始消息兜底（别名也要认）
        raw = resolve_greedy_args(event, args, cmd_name, *(a for a in aliases if a))
        question = (raw or "").strip()

        if not question or question.lower() in HELP_WORDS:
            yield event.plain_result(CHAT_USAGE)
            return

        first, _, rest = question.partition(" ")
        if not channel_mode and first.strip().lower() in CLEAR_WORDS:
            cleared = 0
            if self.ctx_mgr is not None:
                try:
                    cleared = await self.ctx_mgr.clear_personal(umo, uid)
                except Exception as e:
                    logger.warning(f"【AI向导】清空上下文失败: {e}")
            yield event.plain_result(
                f"🧹 已忘掉你这边 {cleared} 条对话记录，下次提问从零开始。"
            )
            return

        force_deep = False
        if first.strip().lower() in DEEP_WORDS and rest.strip():
            force_deep = True
            question = rest.strip()

        player = None
        try:
            player = await self.db.get_player_by_id(uid)
        except Exception as e:
            logger.debug(f"【AI向导】读取玩家失败（按未注册处理）: {e}")
        if player is None and not bool(self._ai_config().get("ALLOW_UNREGISTERED", True)):
            yield event.plain_result("❌ 道友尚未踏入仙途，请先发送 /我要修仙 创建角色。")
            return

        result = await mgr.ask(
            question=question,
            umo=umo,
            uid=uid,
            name=name,
            channel=channel,
            mode=MODE_CHANNEL if channel_mode else MODE_PERSONAL,
            force_deep=force_deep,
            player=player,
            is_admin=self._is_admin(event),
        )

        if not result.messages:
            yield event.plain_result(CHAT_USAGE)
            return

        messages = list(result.messages)
        if result.ok and result.deep and result.sources:
            messages[-1] = f"{messages[-1]}\n\n📚 依据：{'；'.join(result.sources[:3])}"
        prefix = "🔍 深度查阅 · " if (result.ok and result.deep) else "🤖 "
        for idx, msg in enumerate(messages):
            text = f"{prefix}{msg}" if idx == 0 else msg
            yield event.plain_result(text)

    # ---------- /新手指南 ----------

    async def handle_newbie_guide(self, event: AstrMessageEvent):
        """输出《新手引导》：优先长图，失败降级文本。"""
        image = await self._newbie_image()
        if image:
            yield event.image_result(image)
            return
        text = newbie_text()
        if not text:
            text = (
                "🌱 新手三步走：\n"
                "1. /我要修仙 剑修 创建角色\n"
                "2. /签到 → /闭关 → /出关 攒修为\n"
                "3. /突破信息 看门槛，够了就 /突破\n"
                "更多玩法发 /修仙帮助，或直接 /chat <问题> 问我。"
            )
        yield event.plain_result(text)

    async def _newbie_image(self):
        try:
            from ..utils.image_generator import card_enabled

            if not card_enabled("CARD_NEWBIE"):
                return None
            cards = self.config.get("IMAGE_CARDS", {}) or {}
            auto_regen = bool(cards.get("STATIC_AUTO_REGEN", True))
            return await ensure_newbie_page(allow_generate=auto_regen)
        except Exception as e:
            logger.warning(f"【新手指南】图片获取失败，降级为文本: {e}")
            return None
