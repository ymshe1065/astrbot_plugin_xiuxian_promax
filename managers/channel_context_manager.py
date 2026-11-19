# managers/channel_context_manager.py
"""AI 向导的对话上下文缓冲：个人 + 频道双缓冲。

为什么不用 AstrBot 的 ``conversation_manager``：网页聊天室的 UMO 是
``webroom:group:<room_id>``（房间级），直接拿会话历史会把所有玩家混在一起；
而 ``/reset`` 又会把它清空。所以插件自建两份互相独立的缓冲：

| 缓冲 | key | 内容 | 落库 |
|---|---|---|---|
| 个人 | ``umo + uid`` | 本人发言 + 系统对他的回复 + 本轮问答 | ``system_config``（不升 DB 版本） |
| 频道 | ``umo + 频道`` | 近期所有人发言与公开回复 | 仅内存 |

隐私红线：网页端「仅自己 / 看板」回复（储物戒、银行、我的信息…）**只进个人缓冲**，
绝不进频道缓冲，否则等于把别人的资产贴到公屏。
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

try:
    from astrbot.api import logger
except ImportError:  # 允许脱离 AstrBot 跑离线测试
    import logging

    logger = logging.getLogger("xiuxian.chat_context")

__all__ = ["ChatContextManager", "ContextEntry", "clip_text", "personal_key", "channel_key"]

ROLE_USER = "u"
ROLE_SYSTEM = "s"

DEFAULT_PERSONAL_MAX = 20
DEFAULT_PERSONAL_TTL = 30 * 60
DEFAULT_CHANNEL_MAX = 60
DEFAULT_CHANNEL_TTL = 15 * 60
DEFAULT_CLIP = 200
SAVE_INTERVAL = 60.0
MAX_TRACKED = 512
IMAGE_PLACEHOLDER = "[图片]"


def clip_text(text: Any, limit: int = DEFAULT_CLIP) -> str:
    """把一条消息压成单行摘要。

    长战报/长面板只保留首行结论——上下文里塞满逐回合日志既费 token 又没用。
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return ""
    limit = max(20, int(limit or DEFAULT_CLIP))
    if len(lines) == 1:
        return lines[0][:limit]
    if len(lines) >= 6:  # 战报、排行榜、长清单
        return f"{lines[0][:limit]}…"
    return " / ".join(lines)[:limit]


def personal_key(umo: str, uid: str) -> str:
    return f"{umo or ''}|{uid or ''}"


def channel_key(umo: str, channel: str = "") -> str:
    channel = str(channel or "").strip()
    return f"{umo or ''}#{channel}" if channel else (umo or "")


def storage_key(pkey: str) -> str:
    digest = hashlib.sha1(pkey.encode("utf-8")).hexdigest()[:16]
    return f"ai_chat_ctx_{digest}"


@dataclass
class ContextEntry:
    """缓冲里的一条消息。"""

    role: str
    text: str
    uid: str = ""
    name: str = ""
    ts: int = 0

    def to_dict(self) -> Dict[str, str]:
        return {"r": self.role, "t": self.text, "u": self.uid, "n": self.name, "s": str(self.ts)}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional["ContextEntry"]:
        if not isinstance(data, dict):
            return None
        text = str(data.get("t") or "").strip()
        role = str(data.get("r") or ROLE_USER)
        if not text or role not in (ROLE_USER, ROLE_SYSTEM):
            return None
        try:
            ts = int(data.get("s") or 0)
        except (TypeError, ValueError):
            ts = 0
        return cls(role=role, text=text, uid=str(data.get("u") or ""), name=str(data.get("n") or ""), ts=ts)

    def label(self) -> str:
        return "系统" if self.role == ROLE_SYSTEM else (self.name or self.uid or "玩家")


@dataclass
class _Buffer:
    entries: List[ContextEntry] = field(default_factory=list)
    loaded: bool = False
    dirty: bool = False
    last_save: float = 0.0
    last_touch: float = 0.0


class ChatContextManager:
    """个人 / 频道上下文双缓冲。所有写入都不会抛异常，采集失败只降级不阻断。"""

    def __init__(
        self,
        db: Any = None,
        personal_max: int = DEFAULT_PERSONAL_MAX,
        personal_ttl: int = DEFAULT_PERSONAL_TTL,
        channel_max: int = DEFAULT_CHANNEL_MAX,
        channel_ttl: int = DEFAULT_CHANNEL_TTL,
        clip: int = DEFAULT_CLIP,
        persist: bool = True,
    ):
        self.db = db
        self.personal_max = max(4, int(personal_max or DEFAULT_PERSONAL_MAX))
        self.personal_ttl = max(60, int(personal_ttl or DEFAULT_PERSONAL_TTL))
        self.channel_max = max(8, int(channel_max or DEFAULT_CHANNEL_MAX))
        self.channel_ttl = max(60, int(channel_ttl or DEFAULT_CHANNEL_TTL))
        self.clip = max(40, int(clip or DEFAULT_CLIP))
        self.persist = bool(persist) and db is not None
        self._personal: Dict[str, _Buffer] = {}
        self._channel: Dict[str, _Buffer] = {}
        self._lock = asyncio.Lock()
        self._loaded_keys: set = set()

    # ---------- 配置 ----------

    def configure(self, cfg: Optional[Dict[str, Any]]) -> None:
        """从 ``AI_CHAT`` 配置段刷新容量与 TTL（改配置重启即生效）。"""
        if not isinstance(cfg, dict):
            return

        def _num(key: str, default: int, minimum: int) -> int:
            try:
                return max(minimum, int(cfg.get(key, default)))
            except (TypeError, ValueError):
                return default

        if "CONTEXT_MAX" in cfg:
            self.personal_max = _num("CONTEXT_MAX", DEFAULT_PERSONAL_MAX, 4)
        if "CONTEXT_TTL_MIN" in cfg:
            self.personal_ttl = _num("CONTEXT_TTL_MIN", DEFAULT_PERSONAL_TTL // 60, 1) * 60
        if "CHANNEL_CONTEXT_MAX" in cfg:
            self.channel_max = _num("CHANNEL_CONTEXT_MAX", DEFAULT_CHANNEL_MAX, 8)
        if "CHANNEL_CONTEXT_TTL_MIN" in cfg:
            self.channel_ttl = _num("CHANNEL_CONTEXT_TTL_MIN", DEFAULT_CHANNEL_TTL // 60, 1) * 60
        if "CONTEXT_CLIP" in cfg:
            self.clip = _num("CONTEXT_CLIP", DEFAULT_CLIP, 40)

    # ---------- 写入 ----------

    async def record_user(
        self, umo: str, uid: str, name: str = "", text: Any = "", channel: str = ""
    ) -> None:
        """记录玩家发言：个人缓冲 + 频道缓冲都进（发言本来就是公开的）。"""
        clipped = clip_text(text, self.clip)
        if not clipped or not uid:
            return
        try:
            await self._append(
                personal_key(umo, uid),
                ContextEntry(ROLE_USER, clipped, uid=str(uid), name=str(name or ""), ts=int(time.time())),
                self.personal_max,
                personal=True,
            )
            await self._append(
                channel_key(umo, channel),
                ContextEntry(ROLE_USER, clipped, uid=str(uid), name=str(name or ""), ts=int(time.time())),
                self.channel_max,
                personal=False,
            )
        except Exception as e:  # 采集失败绝不能影响正常指令
            logger.debug(f"【AI向导】记录玩家发言失败: {e}")

    async def record_system(
        self,
        umo: str,
        uid: str,
        text: Any = "",
        channel: str = "",
        private: bool = False,
        name: str = "",
    ) -> None:
        """记录系统回复。

        ``private=True``（网页端「仅自己」/看板卡片）只进个人缓冲，
        避免储物戒、银行余额等私密结果被同频道其他人的 ``/chatall`` 读到。
        """
        clipped = clip_text(text, self.clip)
        if not clipped or not uid:
            return
        try:
            await self._append(
                personal_key(umo, uid),
                ContextEntry(ROLE_SYSTEM, clipped, uid=str(uid), name=str(name or ""), ts=int(time.time())),
                self.personal_max,
                personal=True,
            )
            if not private:
                await self._append(
                    channel_key(umo, channel),
                    ContextEntry(ROLE_SYSTEM, clipped, uid=str(uid), ts=int(time.time())),
                    self.channel_max,
                    personal=False,
                )
        except Exception as e:
            logger.debug(f"【AI向导】记录系统回复失败: {e}")

    async def record_image_only(self, umo: str, uid: str, channel: str = "", private: bool = False) -> None:
        """纯图片回复（卡片/长图）在上下文里留个占位，AI 才知道「已经给过图了」。"""
        await self.record_system(
            umo, uid, IMAGE_PLACEHOLDER, channel=channel, private=private
        )

    async def _append(self, key: str, entry: ContextEntry, cap: int, personal: bool) -> None:
        store = self._personal if personal else self._channel
        buf = store.get(key)
        if buf is None:
            self._evict_if_full(store)
            buf = _Buffer()
            store[key] = buf
        if personal and self.persist and not buf.loaded and key not in self._loaded_keys:
            await self._load(buf, key)
        buf.entries.append(entry)
        ttl = self.personal_ttl if personal else self.channel_ttl
        self._prune(buf, cap, ttl)
        buf.last_touch = time.time()
        if personal and self.persist:
            buf.dirty = True
            if buf.last_save and time.time() - buf.last_save >= SAVE_INTERVAL:
                await self._save(buf, key)

    def _prune(self, buf: _Buffer, cap: int, ttl: int) -> None:
        cutoff = int(time.time()) - ttl
        entries = [e for e in buf.entries if e.ts >= cutoff]
        if len(entries) > cap:
            entries = entries[-cap:]
        if len(entries) != len(buf.entries):
            buf.entries = entries
            buf.dirty = True

    def _evict_if_full(self, store: Dict[str, _Buffer]) -> None:
        """内存兜底：跟踪的 key 过多时按最后活跃时间淘汰（淘汰前先落盘）。"""
        if len(store) < MAX_TRACKED:
            return
        oldest = sorted(store.items(), key=lambda kv: kv[1].last_touch)[: max(1, len(store) // 4)]
        for key, buf in oldest:
            store.pop(key, None)
            self._loaded_keys.discard(key)
            if buf.dirty and self.persist:
                try:
                    asyncio.get_running_loop().create_task(self._save(buf, key))
                except RuntimeError:  # 没有运行中的事件循环（离线测试）就放弃这次落盘
                    buf.dirty = False

    # ---------- 持久化（system_config，不动 DB 版本） ----------

    async def _load(self, buf: _Buffer, key: str) -> None:
        self._loaded_keys.add(key)
        if not self.persist:
            buf.loaded = True
            return
        try:
            raw = await self.db.ext.get_system_config(storage_key(key))
            buf.loaded = True
            if not raw:
                return
            import json

            data = json.loads(raw)
            if not isinstance(data, list):
                return
            cutoff = int(time.time()) - self.personal_ttl
            entries = [e for e in (ContextEntry.from_dict(d) for d in data) if e and e.ts >= cutoff]
            buf.entries = entries[-self.personal_max :]
        except Exception as e:
            buf.loaded = True
            logger.debug(f"【AI向导】个人上下文读取失败（按空上下文继续）: {e}")

    async def _save(self, buf: _Buffer, key: str) -> None:
        if not self.persist:
            return
        try:
            import json

            payload = json.dumps(
                [e.to_dict() for e in buf.entries[-self.personal_max :]],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            await self.db.ext.set_system_config(storage_key(key), payload)
            buf.dirty = False
            buf.last_save = time.time()
        except Exception as e:
            logger.debug(f"【AI向导】个人上下文写入失败: {e}")

    async def flush(self) -> int:
        """进程退出前把脏缓冲落盘，返回写入条数。"""
        if not self.persist:
            return 0
        saved = 0
        async with self._lock:
            for key, buf in list(self._personal.items()):
                if buf.dirty:
                    await self._save(buf, key)
                    saved += 1
        return saved

    # ---------- 读取 ----------

    async def personal_entries(
        self, umo: str, uid: str, limit: Optional[int] = None
    ) -> List[ContextEntry]:
        key = personal_key(umo, uid)
        async with self._lock:
            buf = self._personal.get(key)
            if buf is None:
                buf = _Buffer()
                self._personal[key] = buf
            if self.persist and not buf.loaded and key not in self._loaded_keys:
                await self._load(buf, key)
            self._prune(buf, self.personal_max, self.personal_ttl)
            entries = list(buf.entries)
        limit = int(limit or self.personal_max)
        return entries[-limit:]

    def channel_entries(
        self, umo: str, channel: str = "", limit: Optional[int] = None
    ) -> List[ContextEntry]:
        key = channel_key(umo, channel)
        buf = self._channel.get(key)
        if buf is None:
            return []
        self._prune(buf, self.channel_max, self.channel_ttl)
        limit = int(limit or self.channel_max)
        return list(buf.entries[-limit:])

    async def render_personal(self, umo: str, uid: str, limit: Optional[int] = None) -> str:
        entries = await self.personal_entries(umo, uid, limit)
        return "\n".join(f"[{e.label()}] {e.text}" for e in entries)

    def render_channel(self, umo: str, channel: str = "", limit: Optional[int] = None) -> str:
        """频道上下文：**必须带用户名**，否则 AI 分不清谁在问什么。"""
        entries = self.channel_entries(umo, channel, limit)
        lines: List[str] = []
        for e in entries:
            if e.role == ROLE_SYSTEM:
                who = e.name or e.uid or "玩家"
                lines.append(f"[系统→{who}] {e.text}")
            else:
                lines.append(f"[{e.label()}] {e.text}")
        return "\n".join(lines)

    async def clear_personal(self, umo: str, uid: str) -> int:
        """``/chat 清空``：抹掉本人上下文（含已落盘的部分）。"""
        key = personal_key(umo, uid)
        async with self._lock:
            buf = self._personal.get(key)
            count = len(buf.entries) if buf else 0
            if buf is not None:
                buf.entries = []
                buf.loaded = True
                buf.dirty = False
            self._loaded_keys.add(key)
        if self.persist:
            try:
                await self.db.ext.set_system_config(storage_key(key), "[]")
            except Exception as e:
                logger.debug(f"【AI向导】清空个人上下文失败: {e}")
        return count

    def stats(self) -> Dict[str, int]:
        return {
            "personal_keys": len(self._personal),
            "channel_keys": len(self._channel),
            "personal_entries": sum(len(b.entries) for b in self._personal.values()),
            "channel_entries": sum(len(b.entries) for b in self._channel.values()),
        }
