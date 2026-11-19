# managers/ai_chat_manager.py
"""AI 向导（/chat · /chatall）：分层知识库检索 + 显式深度检索 + 限流降级。

硬约束：**AI 的回答只能来自本仓库维护的玩家文档**。检索不到就直说没有，
不允许编数值——玩家的灵石、修为、成功率都是真金白银，编错一次就没人再信它。

流程见 ``docs/design/AI向导与知识库检索设计方案.md``：

    限流 → 玩家快照 → 核心手册/新手引导 → 浅层检索
      → (显式/低分触发) mini agent 扩展关键词 → 多路深度检索
      → 组装提示词 → LLM → 清洗截断 → (可选)拆条
任何一步失败都退回固定话术 + 指向 /修仙帮助、/修仙诊断。
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    from astrbot.api import logger
except ImportError:  # 允许脱离 AstrBot 跑离线测试
    import logging

    logger = logging.getLogger("xiuxian.ai_chat")

from ..utils.knowledge_retriever import ChunkHit, format_hits, get_retriever

__all__ = ["AIChatManager", "AIChatResult", "DEFAULT_AI_CHAT_CONFIG", "sanitize_answer"]

MODE_PERSONAL = "personal"
MODE_CHANNEL = "channel"

# 玩家说这些词，说明他要的是「查资料」而不是「随口聊」，走深度检索
DEEP_KEYWORDS: Tuple[str, ...] = (
    "帮我查", "查阅", "查找", "查一下", "查询", "解答", "解释", "详细说明",
    "详细介绍", "深入", "全面介绍", "全面", "机制", "原理", "为什么", "怎么回事",
)

DEFAULT_AI_CHAT_CONFIG: Dict[str, Any] = {
    "ENABLED": True,
    "PROVIDER_ID": "",
    "TIMEOUT": 20,
    "CONCURRENCY": 3,
    "COOLDOWN_SEC": 10,
    "DAILY_LIMIT": 30,
    "GROUP_DAILY_LIMIT": 300,
    "DEEP_ENABLED": True,
    "DEEP_TIMEOUT": 30,
    "DEEP_COOLDOWN_SEC": 30,
    "DEEP_DAILY_LIMIT": 10,
    "DEEP_ON_LOW_SCORE": True,
    "DEEP_SCORE_THRESHOLD": 8.0,
    "DEEP_AGENT_TIMEOUT": 8,
    "DEEP_TOP_K": 9,
    "KB_TOP_K": 5,
    "KB_MAX_CHARS": 6000,
    "KB_CORE_MAX_CHARS": 7000,
    "CONTEXT_LIMIT": 12,
    "CHANNEL_CONTEXT_LIMIT": 24,
    "MAX_QUESTION_CHARS": 300,
    "MAX_ANSWER_CHARS": 400,
    "DEEP_MAX_ANSWER_CHARS": 800,
    "SPLIT_LONG_ANSWER": True,
    "MAX_TOTAL_CHARS": 16000,
    "INCLUDE_PLAYER_STATE": True,
    "ALLOW_UNREGISTERED": True,
    "CHATALL_ENABLED": True,
    "NEWBIE_HINT": True,
    "NEWBIE_DAYS": 7,
    "NEWBIE_MAX_LEVEL_INDEX": 5,
    "NEWBIE_MAX_TUTORIAL_STEP": 4,
}

FALLBACK_NO_PROVIDER = (
    "🤖 AI 向导还没接通大模型：请先在 AstrBot 的「平台/模型」里配置对话 Provider，"
    "或在插件配置 AI_CHAT.PROVIDER_ID 指定一个。\n"
    "现在可以先用 /修仙帮助 查指令、/新手指南 看上手流程、/修仙诊断 体检账号。"
)
FALLBACK_TIMEOUT = (
    "⏳ AI 向导这次想得太久超时了。稍后再试一次，"
    "或者换个更具体的问法（例如「/chat 突破失败会掉境界吗」）。\n"
    "急用可查 /修仙帮助 · /新手指南 · /修仙诊断。"
)
FALLBACK_ERROR = (
    "😵 AI 向导这次没能答上来（模型调用异常）。请稍后再试，"
    "或先用 /修仙帮助 查指令、/新手指南 看流程。"
)
FALLBACK_EMPTY = (
    "🤔 我在玩家文档里没找到能支撑答案的内容，不敢瞎编。"
    "可以试试：把问题说得更具体、加上指令名（如 /突破），"
    "或用 /chat 深度 <问题> 让我查得更细。"
)
NO_KNOWLEDGE_SUFFIX = "\n\n（资料里没有明确写，以上为通用建议，具体数值以游戏内为准。）"

_ANSWER_LIMIT_NOTICE = "…"


def sanitize_answer(text: Any, max_chars: int = 400) -> str:
    """清洗模型输出：去 Markdown 记号、压掉多余空行、按句截断。

    QQ 端不渲染 Markdown，``**``、``###``、``- `` 只会变成噪音；
    截断优先落在句末标点，别把半句话甩给玩家。
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    raw = re.sub(r"```[a-zA-Z]*\n?", "", raw)
    raw = re.sub(r"^#{1,6}\s*", "", raw, flags=re.M)
    raw = raw.replace("**", "").replace("__", "")
    raw = re.sub(r"`", "", raw)
    raw = re.sub(r"^\s*[-*+]\s+", "· ", raw, flags=re.M)
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    raw = "\n".join(ln.rstrip() for ln in raw.splitlines()).strip()
    if not raw:
        return ""
    limit = max(60, int(max_chars or 400))
    if len(raw) <= limit:
        return raw
    cut = raw[:limit]
    for sep in ("\n\n", "。", "；", "！", "？", "\n", ".", " "):
        idx = cut.rfind(sep)
        if idx >= int(limit * 0.55):
            cut = cut[: idx + len(sep)]
            break
    return cut.rstrip() + _ANSWER_LIMIT_NOTICE


def split_answer(text: str, max_chars: int) -> List[str]:
    """长回答拆条发送（深度模式最多 800 字，一条塞不下也读着累）。"""
    text = str(text or "").strip()
    if not text or len(text) <= max_chars:
        return [text] if text else []
    parts: List[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_chars:
            parts.append(remaining)
            break
        window = remaining[:max_chars]
        cut = max_chars
        for sep in ("\n\n", "\n", "。", "；", " "):
            idx = window.rfind(sep)
            if idx >= int(max_chars * 0.5):
                cut = idx + len(sep)
                break
        parts.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    return [p for p in parts if p]


@dataclass
class AIChatResult:
    """一次问答的结果：消息列表（可能拆成多条）+ 诊断信息。"""

    ok: bool = False
    messages: List[str] = field(default_factory=list)
    notice: str = ""
    deep: bool = False
    sources: List[str] = field(default_factory=list)
    elapsed: float = 0.0
    error: str = ""
    degraded: bool = False

    @property
    def text(self) -> str:
        return "\n\n".join(self.messages)


class AIChatManager:
    """把「玩家一句话」变成「有据可查的回答」。"""

    def __init__(
        self,
        context: Any = None,
        db: Any = None,
        config_manager: Any = None,
        ctx_mgr: Any = None,
        config: Optional[Dict[str, Any]] = None,
        retriever: Any = None,
    ):
        self.context = context
        self.db = db
        self.config_manager = config_manager
        self.ctx_mgr = ctx_mgr
        self.retriever = retriever or get_retriever()
        self.cfg: Dict[str, Any] = dict(DEFAULT_AI_CHAT_CONFIG)
        self.configure(config)
        self._semaphore = asyncio.Semaphore(max(1, int(self.cfg.get("CONCURRENCY", 3))))
        self._cooldown: Dict[str, float] = {}
        self._daily: Dict[str, Dict[str, int]] = {}
        self._group_daily: Dict[str, Dict[str, int]] = {}

    # ---------- 配置 ----------

    def configure(self, config: Optional[Dict[str, Any]]) -> None:
        """从插件配置的 ``AI_CHAT`` 段刷新参数；缺项一律用默认值。"""
        section = config.get("AI_CHAT", config) if isinstance(config, dict) else None
        if not isinstance(section, dict):
            return
        merged = dict(self.cfg)
        for key, default in DEFAULT_AI_CHAT_CONFIG.items():
            if key not in section:
                continue
            value = section.get(key)
            if isinstance(default, bool):
                merged[key] = bool(value)
            elif isinstance(default, int):
                try:
                    merged[key] = int(value)
                except (TypeError, ValueError):
                    merged[key] = default
            elif isinstance(default, float):
                try:
                    merged[key] = float(value)
                except (TypeError, ValueError):
                    merged[key] = default
            else:
                merged[key] = "" if value is None else str(value)
        merged["CONCURRENCY"] = max(1, min(8, merged.get("CONCURRENCY", 3)))
        merged["TIMEOUT"] = max(5, min(120, merged.get("TIMEOUT", 20)))
        merged["DEEP_TIMEOUT"] = max(merged["TIMEOUT"], min(180, merged.get("DEEP_TIMEOUT", 30)))
        self.cfg = merged
        self._semaphore = asyncio.Semaphore(merged["CONCURRENCY"])
        if self.ctx_mgr is not None and hasattr(self.ctx_mgr, "configure"):
            try:
                self.ctx_mgr.configure(section)
            except Exception as e:
                logger.debug(f"【AI向导】上下文参数刷新失败: {e}")

    def _int(self, key: str, default: int) -> int:
        try:
            return int(self.cfg.get(key, default))
        except (TypeError, ValueError):
            return default

    def enabled(self) -> bool:
        return bool(self.cfg.get("ENABLED", True))

    def chatall_enabled(self) -> bool:
        return bool(self.cfg.get("ENABLED", True)) and bool(self.cfg.get("CHATALL_ENABLED", True))

    # ---------- 限流 ----------

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y%m%d")

    def _bump(self, store: Dict[str, Dict[str, int]], key: str, field: str) -> int:
        today = self._today()
        bucket = store.get(key)
        if not bucket or bucket.get("date") != today:
            bucket = {"date": today}
            store[key] = bucket
        bucket[field] = int(bucket.get(field, 0)) + 1
        return bucket[field]

    def _count(self, store: Dict[str, Dict[str, int]], key: str, field: str) -> int:
        bucket = store.get(key) or {}
        if bucket.get("date") != self._today():
            return 0
        return int(bucket.get(field, 0))

    def check_rate_limit(self, uid: str, group_key: str, deep: bool) -> str:
        """返回空串表示放行，否则返回给玩家的拒绝话术。"""
        now = time.time()
        cooldown = self._int("DEEP_COOLDOWN_SEC", 30) if deep else self._int("COOLDOWN_SEC", 10)
        last = self._cooldown.get(uid, 0.0)
        if cooldown > 0 and now - last < cooldown:
            return f"⏳ 问得太急了，{int(cooldown - (now - last)) + 1} 秒后再问我一次吧。"

        daily_limit = self._int("DAILY_LIMIT", 30)
        if daily_limit > 0 and self._count(self._daily, uid, "total") >= daily_limit:
            return f"🌙 你今天已经问了我 {daily_limit} 次，明天再来找我论道吧（/修仙帮助 也能查指令）。"
        if deep:
            deep_limit = self._int("DEEP_DAILY_LIMIT", 10)
            if deep_limit > 0 and self._count(self._daily, uid, "deep") >= deep_limit:
                return f"🌙 深度查阅每天限 {deep_limit} 次，今天已用完；普通提问 /chat <问题> 仍可继续。"
        group_limit = self._int("GROUP_DAILY_LIMIT", 300)
        if group_limit > 0 and group_key and self._count(self._group_daily, group_key, "total") >= group_limit:
            return "🌙 本频道今天的 AI 问答次数已用完，明天再试。"
        return ""

    def _consume(self, uid: str, group_key: str, deep: bool) -> None:
        self._cooldown[uid] = time.time()
        self._bump(self._daily, uid, "total")
        if deep:
            self._bump(self._daily, uid, "deep")
        if group_key:
            self._bump(self._group_daily, group_key, "total")

    # ---------- Provider ----------

    async def resolve_provider(self, umo: str) -> str:
        """优先用配置里指定的 Provider，否则取该会话当前使用的对话模型。"""
        configured = str(self.cfg.get("PROVIDER_ID") or "").strip()
        if configured:
            return configured
        if self.context is None:
            return ""
        getter = getattr(self.context, "get_current_chat_provider_id", None)
        if getter is None:
            return ""
        try:
            provider_id = getter(umo)
            if asyncio.iscoroutine(provider_id):
                provider_id = await provider_id
            return str(provider_id or "")
        except Exception as e:  # ProviderNotFoundError 等
            logger.info(f"【AI向导】未找到可用对话模型：{e}")
            return ""

    # ---------- 玩家快照 ----------

    def is_newbie(self, player: Any) -> bool:
        """新玩家整篇注入《新手引导》，比核心手册更好用。"""
        if player is None:
            return True
        try:
            if int(getattr(player, "tutorial_step", 0) or 0) < self._int("NEWBIE_MAX_TUTORIAL_STEP", 4):
                return True
            if int(getattr(player, "level_index", 0) or 0) <= self._int("NEWBIE_MAX_LEVEL_INDEX", 5):
                return True
            created = int(getattr(player, "created_at", 0) or 0)
            if created and (time.time() - created) <= self._int("NEWBIE_DAYS", 7) * 86400:
                return True
        except (TypeError, ValueError):
            return True
        return False

    def player_snapshot(self, player: Any) -> str:
        """只给修行状态，**不含灵石与资产**——回答是发在公屏的。"""
        if not player or not self.cfg.get("INCLUDE_PLAYER_STATE", True):
            return ""
        try:
            level = ""
            need = 0
            if self.config_manager is not None:
                try:
                    level = player.get_level(self.config_manager)
                    need = int(player.get_required_exp(self.config_manager) or 0)
                except Exception:
                    level = ""
            exp = int(getattr(player, "experience", 0) or 0)
            progress = f"{exp:,}"
            if need > 0:
                percent = max(0, min(100, int(exp * 100 / need)))
                progress = f"{exp:,} / {need:,}（{percent}%）"
            lines = [
                f"道号：{getattr(player, 'user_name', '') or player.user_id or '未知道友'}",
                f"境界：{level or '未知'}",
                f"职业：{getattr(player, 'cultivation_type', '') or '未知'}",
                f"灵根：{getattr(player, 'spiritual_root', '') or '未知'}",
                f"当前状态：{getattr(player, 'state', '') or '空闲'}",
                f"修为进度：{progress}",
            ]
            karma = int(getattr(player, "karma", 0) or 0)
            lines.append(f"功德/业力：{karma:+d}")
            return "\n".join(lines)
        except Exception as e:
            logger.debug(f"【AI向导】玩家快照生成失败: {e}")
            return ""

    # ---------- 深度检索 ----------

    @staticmethod
    def wants_deep(question: str, force: bool = False) -> bool:
        if force:
            return True
        text = str(question or "")
        return any(k in text for k in DEEP_KEYWORDS)

    async def expand_queries(
        self, provider_id: str, question: str, hints: Sequence[str], history: str
    ) -> List[str]:
        """Mini Agent：只拆检索词，不作答。失败/超时一律返回空列表，退回浅层结果。"""
        if not provider_id or self.context is None:
            return []
        prompt = (
            "你是检索关键词扩展器。根据玩家问题与已命中的资料标题，输出 3-6 个用于全文检索的中文关键词。\n"
            "严格规则：\n"
            "1. 每行一个关键词，不要编号、不要标点、不要解释、不要回答问题。\n"
            "2. 关键词要具体，优先游戏内术语（如「破境丹」「押镖」「灵田枯萎」），不要泛词（如「方法」「问题」）。\n"
            "3. 不要重复玩家原话里已有的词，要换说法、拆概念。\n"
            f"【已命中标题】\n{chr(10).join(f'- {h}' for h in hints) or '（无）'}\n"
            f"【最近对话】\n{history or '（无）'}\n"
            f"【玩家问题】\n{question}\n"
            "【关键词】"
        )
        timeout = max(3, self._int("DEEP_AGENT_TIMEOUT", 8))
        # max_tokens/temperature 是 OpenAI 兼容参数，个别 Provider 不认；失败就去掉重试一次
        kwargs: Dict[str, Any] = {"max_tokens": 200, "temperature": 0.2}
        for attempt in (0, 1):
            call_kwargs = kwargs if attempt == 0 else {}
            try:
                resp = await asyncio.wait_for(
                    self.context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=prompt,
                        system_prompt="只输出关键词，每行一个。",
                        **call_kwargs,
                    ),
                    timeout=timeout,
                )
                raw = str(getattr(resp, "completion_text", "") or "")
                words: List[str] = []
                for line in raw.splitlines():
                    word = re.sub(r"^[\s\-\*\d\.、,，:：]+", "", line).strip()
                    word = word.strip("`\"'“”‘’ ")
                    if not word or len(word) > 20:
                        continue
                    if word not in words:
                        words.append(word)
                    if len(words) >= 6:
                        break
                return words
            except asyncio.TimeoutError:
                logger.info("【AI向导】深度检索的关键词扩展超时，退回浅层结果")
                return []
            except Exception as e:
                if attempt == 0 and call_kwargs:
                    logger.debug(f"【AI向导】关键词扩展带采样参数失败，去掉参数重试: {e}")
                    continue
                logger.info(f"【AI向导】关键词扩展失败，退回浅层结果: {e}")
                return []
        return []

    # ---------- 知识装配 ----------

    def _collect_knowledge(
        self,
        question: str,
        player: Any,
        deep: bool,
        budget: int,
        is_admin: bool = False,
    ) -> Tuple[str, List[ChunkHit], float]:
        """返回 (固定手册, 检索命中, 最高分)。"""
        retriever = self.retriever
        core = ""
        try:
            if self.is_newbie(player):
                core = retriever.newbie_guide(max_chars=min(budget, self._int("KB_CORE_MAX_CHARS", 7000)))
            if not core:
                core = retriever.core_manual(max_chars=min(budget, self._int("KB_CORE_MAX_CHARS", 7000)))
        except Exception as e:
            logger.warning(f"【AI向导】核心手册装配失败: {e}")

        top_k = self._int("DEEP_TOP_K", 9) if deep else self._int("KB_TOP_K", 5)
        max_chars = max(800, budget - len(core))
        hits: List[ChunkHit] = []
        try:
            hits = retriever.search(
                question, top_k=top_k, max_chars=max_chars, include_admin=is_admin
            )
            if not hits:
                hits = []
        except Exception as e:
            logger.warning(f"【AI向导】知识库检索失败: {e}")

        # 问题里点到具体物品时，精确条目优先于模糊检索
        try:
            names = self._item_mentions(question)
            if names:
                exact = retriever.lookup_items(names, limit=3)
                seen = {h.chunk.chunk_id for h in hits}
                for hit in exact:
                    if hit.chunk.chunk_id not in seen:
                        hits.insert(0, hit)
                        seen.add(hit.chunk.chunk_id)
                hits = hits[: top_k + 2]
        except Exception as e:
            logger.debug(f"【AI向导】物品条目匹配失败: {e}")

        best = max((h.score for h in hits), default=0.0)
        return core, hits, best

    def _item_mentions(self, question: str) -> List[str]:
        """从问题里认出图鉴条目名，用于精确条目优先于模糊检索。"""
        try:
            return self.retriever.match_items(question, limit=3)
        except Exception as e:
            logger.debug(f"【AI向导】物品名识别失败: {e}")
            return []

    async def deep_search(
        self,
        provider_id: str,
        question: str,
        shallow: Sequence[ChunkHit],
        history: str,
        budget: int,
        is_admin: bool = False,
    ) -> List[ChunkHit]:
        """多路检索合并：扩展词各自检索，被多个词命中的块加权后重排。"""
        hints = [h.chunk.title.split(" · ")[-1] for h in list(shallow)[:3]]
        words = await self.expand_queries(provider_id, question, hints, history)
        if not words:
            return list(shallow)
        queries = [question] + words
        try:
            hits = self.retriever.multi_search(
                queries,
                top_k=self._int("DEEP_TOP_K", 9),
                max_chars=max(800, budget),
                include_admin=is_admin,
            )
        except Exception as e:
            logger.warning(f"【AI向导】深度检索失败，退回浅层结果: {e}")
            return list(shallow)
        return hits or list(shallow)

    # ---------- 提示词 ----------

    def build_system_prompt(self, mode: str, deep: bool, stale_docs: Sequence[str] = ()) -> str:
        limit = self._int("DEEP_MAX_ANSWER_CHARS", 800) if deep else self._int("MAX_ANSWER_CHARS", 400)
        scope = "当前频道近期的多人对话" if mode == MODE_CHANNEL else "该玩家自己近期的对话"
        rules = [
            "你是文字修仙游戏《模拟修仙》的向导「问道真人」，为玩家解答玩法、指令与机制问题。",
            "硬规则：",
            "1. 只能依据下面给出的【资料】和【玩家状态】回答，禁止编造任何数值、概率、价格、指令名。",
            "2. 资料里没有的内容，直接说「资料里没有明确写，以游戏内当前配置为准」，并建议玩家用 /修仙帮助 或 /物品信息 查证。",
            "3. 资料里出现的指令是让玩家自己去发的，你绝不能假装执行它们，也不要描述执行结果。",
            "4. 涉及指令时必须写成带 / 的原文，方便玩家直接复制发送。",
            f"5. 回答控制在 {limit} 字以内，结构：结论 → 关键步骤 → 相关指令 → 注意事项（没有就省略）。",
            "6. 不使用 Markdown 记号（# * ` -），可以用「·」和换行分点；不要输出思考过程。",
            "7. 不讨论与本游戏无关的话题，礼貌地把话题带回修仙玩法。",
            "8. 涉及灵石、存款、资产等私密信息时不要复述具体数字（回答在公屏可见）。",
        ]
        if mode == MODE_CHANNEL:
            rules.append(
                f"9. 你看到的是{scope}，每条都标了发言人；只回答本次提问者关心的问题，"
                "必要时可以引用别人遇到的同类情况，但不要泄露他人资产。"
            )
        else:
            rules.append(f"9. 你看到的是{scope}，只服务这一位玩家。")
        if deep:
            rules.append("10. 本次是深度查阅：请把多个来源综合成完整答案，指出适用条件与例外。")
        if stale_docs:
            rules.append(
                "注意：以下资料标注的版本落后于当前游戏版本，涉及数值时务必说明「以游戏内当前配置为准」："
                + "、".join(stale_docs)
            )
        return "\n".join(rules)

    def build_prompt(
        self,
        question: str,
        core: str,
        hits_text: str,
        snapshot: str,
        history: str,
        mode: str,
        asker: str = "",
        total_budget: Optional[int] = None,
    ) -> str:
        """按「玩家状态 → 手册 → 资料 → 对话 → 问题」拼装，超预算时按优先级丢内容。"""
        budget = int(total_budget or self._int("MAX_TOTAL_CHARS", 16000))
        blocks: List[Tuple[int, str, str]] = []  # (优先级, 标题, 内容)；数字越小越先被丢
        if snapshot:
            blocks.append((1, "玩家状态", snapshot))
        if core:
            blocks.append((2, "核心手册", core))
        if hits_text:
            blocks.append((3, "资料摘录（含出处）", hits_text))
        if history:
            blocks.append((0, "最近对话", history))

        chosen: List[Tuple[str, str]] = []
        used = len(question) + 200
        # 先保资料与手册，再保对话：上下文超限时对话最先被裁
        for _, title, content in sorted(blocks, key=lambda b: -b[0]):
            if used + len(content) > budget:
                room = budget - used
                if room < 400:
                    continue
                content = content[:room] + "\n（已截断）"
            chosen.append((title, content))
            used += len(content)

        parts = [f"【{title}】\n{content}" for title, content in chosen]
        asker_line = f"（提问者：{asker}）" if mode == MODE_CHANNEL and asker else ""
        parts.append(f"【本次问题】{asker_line}\n{question}")
        return "\n\n".join(parts)

    # ---------- 主流程 ----------

    async def ask(
        self,
        question: str,
        umo: str,
        uid: str,
        name: str = "",
        channel: str = "",
        mode: str = MODE_PERSONAL,
        force_deep: bool = False,
        player: Any = None,
        is_admin: bool = False,
    ) -> AIChatResult:
        started = time.time()
        result = AIChatResult()
        question = re.sub(r"\s+", " ", str(question or "")).strip()
        if not question:
            result.notice = "请把问题写在指令后面，例如：/chat 突破失败会掉境界吗"
            result.messages = [result.notice]
            return result

        max_q = self._int("MAX_QUESTION_CHARS", 300)
        if len(question) > max_q:
            question = question[:max_q]

        group_key = f"{umo}#{channel}" if channel else (umo or "")
        deep = bool(self.cfg.get("DEEP_ENABLED", True)) and self.wants_deep(question, force_deep)
        reject = self.check_rate_limit(uid, group_key, deep)
        if reject:
            result.notice = reject
            result.messages = [reject]
            return result

        provider_id = await self.resolve_provider(umo)
        if not provider_id:
            result.error = "no_provider"
            result.messages = [FALLBACK_NO_PROVIDER]
            result.degraded = True
            return result

        async with self._semaphore:
            # 并发排队期间可能已被别人问过；这里再校验一次冷却，避免同一个人连点
            reject = self.check_rate_limit(uid, group_key, deep)
            if reject:
                result.notice = reject
                result.messages = [reject]
                return result

            history = await self._build_history(umo, uid, channel, mode)
            snapshot = self.player_snapshot(player)
            core, hits, best = self._collect_knowledge(
                question, player, False, self._int("MAX_TOTAL_CHARS", 16000) // 2, is_admin
            )

            # 低分自动深挖：浅层几乎没命中，多半是玩家问法与文档用词不一致
            auto_deep = (
                not deep
                and bool(self.cfg.get("DEEP_ENABLED", True))
                and bool(self.cfg.get("DEEP_ON_LOW_SCORE", True))
                and best < float(self.cfg.get("DEEP_SCORE_THRESHOLD", 8.0))
                and self._count(self._daily, uid, "deep") < self._int("DEEP_DAILY_LIMIT", 10)
                and not self.check_rate_limit(uid, group_key, True)
            )
            if auto_deep:
                deep = True
            if deep:
                hits = await self.deep_search(
                    provider_id,
                    question,
                    hits,
                    history[-600:],
                    self._int("KB_MAX_CHARS", 6000),
                    is_admin,
                )
                best = max((h.score for h in hits), default=best)

            hits_text = format_hits(hits, max_chars=self._int("KB_MAX_CHARS", 6000))
            stale: List[str] = []
            try:
                from ..utils.static_pages import HELP_VERSION

                stale = self.retriever.stale_docs(HELP_VERSION)
            except Exception:
                stale = []

            system_prompt = self.build_system_prompt(mode, deep, stale)
            prompt = self.build_prompt(
                question=question,
                core=core,
                hits_text=hits_text,
                snapshot=snapshot,
                history=history,
                mode=mode,
                asker=name,
            )

            timeout = self._int("DEEP_TIMEOUT", 30) if deep else self._int("TIMEOUT", 20)
            # 冷却先记账：即使模型超时/报错，也不给连点刷请求的机会（日配额仍只算成功的那次）
            self._cooldown[uid] = time.time()
            answer = ""
            try:
                resp = await asyncio.wait_for(
                    self.context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=prompt,
                        system_prompt=system_prompt,
                    ),
                    timeout=timeout,
                )
                answer = str(getattr(resp, "completion_text", "") or "")
            except asyncio.TimeoutError:
                logger.warning(f"【AI向导】LLM 超时（{timeout}s），uid={uid}")
                result.error = "timeout"
                result.messages = [FALLBACK_TIMEOUT]
                result.degraded = True
                result.elapsed = time.time() - started
                return result
            except Exception as e:
                logger.warning(f"【AI向导】LLM 调用失败: {e}")
                result.error = f"llm_error:{e}"
                result.messages = [FALLBACK_ERROR]
                result.degraded = True
                result.elapsed = time.time() - started
                return result

            self._consume(uid, group_key, deep)

            limit = self._int("DEEP_MAX_ANSWER_CHARS", 800) if deep else self._int("MAX_ANSWER_CHARS", 400)
            cleaned = sanitize_answer(answer, limit)
            if not cleaned:
                result.error = "empty_answer"
                result.messages = [FALLBACK_EMPTY]
                result.degraded = True
                result.deep = deep
                result.elapsed = time.time() - started
                return result
            if best <= 0 or not hits:
                cleaned += NO_KNOWLEDGE_SUFFIX

            if bool(self.cfg.get("SPLIT_LONG_ANSWER", True)) and len(cleaned) > self._int("MAX_ANSWER_CHARS", 400):
                result.messages = split_answer(cleaned, self._int("MAX_ANSWER_CHARS", 400))[:2]
            else:
                result.messages = [cleaned]
            result.ok = True
            result.deep = deep
            result.sources = [h.chunk.citation for h in hits[:4]]
            result.elapsed = time.time() - started

            # 把这轮问答写回个人上下文，玩家追问时才有连贯性
            await self._remember(umo, uid, name, question, result.messages, channel, mode)
            return result

    async def _build_history(self, umo: str, uid: str, channel: str, mode: str) -> str:
        if self.ctx_mgr is None:
            return ""
        try:
            if mode == MODE_CHANNEL:
                limit = self._int("CHANNEL_CONTEXT_LIMIT", 24)
                return self.ctx_mgr.render_channel(umo, channel, limit)
            limit = self._int("CONTEXT_LIMIT", 12)
            return await self.ctx_mgr.render_personal(umo, uid, limit)
        except Exception as e:
            logger.debug(f"【AI向导】上下文读取失败（按无上下文继续）: {e}")
            return ""

    async def _remember(
        self, umo: str, uid: str, name: str, question: str, messages: Sequence[str], channel: str, mode: str
    ) -> None:
        if self.ctx_mgr is None:
            return
        try:
            await self.ctx_mgr.record_user(umo, uid, name, f"/chat {question}", channel=channel)
            answer = "\n".join(messages)
            # 频道模式下回答是公屏内容，其他人 /chatall 时也该看得到
            await self.ctx_mgr.record_system(
                umo, uid, answer, channel=channel if mode == MODE_CHANNEL else "", private=mode != MODE_CHANNEL
            )
        except Exception as e:
            logger.debug(f"【AI向导】问答写回上下文失败: {e}")

    # ---------- 自检 ----------

    def warmup(self, expected_version: str = "") -> Dict[str, Any]:
        """启动时预热索引并检查文档新鲜度（阻塞操作放线程池由调用方决定）。"""
        info: Dict[str, Any] = {"ok": False, "stale": [], "stats": {}}
        try:
            stats = self.retriever.stats()
            info["stats"] = stats
            info["ok"] = bool(stats.get("chunks"))
            if expected_version:
                info["stale"] = self.retriever.stale_docs(expected_version)
        except Exception as e:
            logger.warning(f"【AI向导】知识库预热失败: {e}")
            info["error"] = str(e)
        return info
