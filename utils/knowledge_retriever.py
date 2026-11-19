# utils/knowledge_retriever.py
"""分层知识库检索：加载 docs/player 下的玩家文档，按标题/物品行分块，
用字符二元组 BM25 做稀疏检索（零第三方依赖）。

设计要点见 ``docs/design/AI向导与知识库检索设计方案.md``：
- 机制/指南类文档按 ``##``/``###``/``####`` 切块，保留标题路径；超长块二次切分；
- 物品图鉴按「一行一物品」切块，只对物品名与标题建索引，避免目录型长文吞掉预算；
- 固定核心手册（快速上手/术语/核心数值/FAQ）单独提供，供每次请求无条件注入；
- 管理员章节默认不参与普通玩家检索；
- 按文件 mtime 懒重建，改文档不用重启。
"""
from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from astrbot.api import logger
except ImportError:  # 允许脱离 AstrBot 跑离线测试
    import logging

    logger = logging.getLogger("xiuxian.knowledge_retriever")

__all__ = ["KnowledgeRetriever", "ChunkHit", "Chunk", "DOC_SPECS", "CORE_SECTIONS"]

DOCS_DIR = Path(__file__).resolve().parents[1] / "docs" / "player"

# (文件名, 权重, 分块方式, 展示简称)
DOC_SPECS: Tuple[Tuple[str, float, str, str], ...] = (
    ("xiuxian_knowledge_base.md", 1.00, "heading", "机制知识库"),
    ("新手引导.md", 1.05, "heading", "新手引导"),
    ("xiuxian_player_guide.md", 0.95, "heading", "玩家指南"),
    ("玩家属性公式表.md", 0.80, "heading", "属性公式"),
    ("xiuxian_item_guide.md", 0.70, "item", "物品图鉴"),
    ("经济基准表.md", 0.60, "heading", "经济基准"),
    ("仙缘商店定价表.md", 0.60, "heading", "仙缘定价"),
)

# 每次请求无条件注入的核心章节（取自机制知识库）
CORE_SECTIONS: Tuple[str, ...] = (
    "核心术语",
    "2. 快速上手",
    "3. 职业与开局",
    "4. 核心数值",
    "5. 修炼与状态",
    "8. 常见问题",
)

CORE_DOC = "xiuxian_knowledge_base.md"
NEWBIE_DOC = "新手引导.md"

# 只有管理员才能检索到的章节关键词
ADMIN_HEADING_KEYWORDS: Tuple[str, ...] = ("管理员", "数据与维护", "管理命令")

MAX_CHUNK_CHARS = 1200
MAX_ITEM_CHUNK_CHARS = 600

_HEADING_RE = re.compile(r"^(#{2,4})\s+(.*)$")
_ITEM_LINE_RE = re.compile(r"^[-*]\s+\*\*(?P<name>[^*]{1,40})\*\*(?P<rest>.*)$")
_PUNCT_RE = re.compile(r"[\s\W_]+", re.UNICODE)
_COMMAND_RE = re.compile(r"/([\u4e00-\u9fa5A-Za-z]{2,12})")

# 玩家口语 → 文档标准说法，弥补稀疏检索的改写召回
SYNONYMS: Dict[str, Tuple[str, ...]] = {
    "打不过": ("战力", "强化", "洗练", "境界"),
    "卡住": ("解卡", "忙碌状态"),
    "没钱": ("灵石", "银行", "贷款"),
    "经验": ("修为",),
    "等级": ("境界",),
    "血量": ("气血",),
    "蓝": ("灵气",),
    "宠物": ("灵兽",),
    "公会": ("宗门",),
    "帮派": ("宗门",),
    "副本": ("秘境", "周常Boss"),
    "挂机": ("闭关", "自动代理"),
    "强化失败": ("强化", "洗练"),
    "转生": ("飞升", "弃道重修"),
    "重开": ("弃道重修",),
    "每日": ("签到", "每日必做"),
    "日常": ("签到", "一键日常"),
    "ai": ("chat", "AI向导"),
    "聊天": ("chat", "AI向导"),
}


def _normalize(text: str) -> str:
    return _PUNCT_RE.sub("", str(text or "")).lower()


def _bigrams(text: str) -> List[str]:
    norm = _normalize(text)
    if not norm:
        return []
    if len(norm) == 1:
        return [norm]
    return [norm[i : i + 2] for i in range(len(norm) - 1)]


def _tf_map(tokens: Iterable[str]) -> Dict[str, int]:
    tf: Dict[str, int] = {}
    for tok in tokens:
        tf[tok] = tf.get(tok, 0) + 1
    return tf


@dataclass
class Chunk:
    """一个可检索的知识块。"""

    chunk_id: str
    doc: str
    label: str
    title: str
    text: str
    kind: str = "heading"
    item_name: str = ""
    admin_only: bool = False
    weight: float = 1.0
    tf: Dict[str, int] = field(default_factory=dict)
    dl: int = 0
    norm_text: str = ""
    norm_title: str = ""
    commands: Tuple[str, ...] = ()

    @property
    def citation(self) -> str:
        return f"《{self.label}》{self.title}"


@dataclass
class ChunkHit:
    """一次检索命中。"""

    chunk: Chunk
    score: float
    matched_queries: int = 1

    @property
    def text(self) -> str:
        return self.chunk.text

    @property
    def citation(self) -> str:
        return self.chunk.citation


def _split_front_matter(raw: str) -> Tuple[Dict[str, str], str]:
    """极简 front matter 解析：只取 ``key: value`` 一层，够用且无依赖。"""
    meta: Dict[str, str] = {}
    if not raw.startswith("---"):
        return meta, raw
    end = raw.find("\n---", 3)
    if end < 0:
        return meta, raw
    header = raw[3:end]
    body = raw[end + 4 :].lstrip("\n")
    for line in header.splitlines():
        if ":" not in line or line.startswith((" ", "\t", "-")):
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    return meta, body


def _is_admin_title(title: str) -> bool:
    return any(k in title for k in ADMIN_HEADING_KEYWORDS)


def _split_long(title: str, text: str, limit: int) -> List[str]:
    """超长块按空行/列表项二次切分，每片仍带标题，避免把整张长表塞进上下文。"""
    if len(text) <= limit:
        return [text]
    blocks: List[str] = []
    buf: List[str] = []
    size = 0
    for para in re.split(r"\n\s*\n", text):
        para = para.strip("\n")
        if not para:
            continue
        if size + len(para) > limit and buf:
            blocks.append("\n".join(buf))
            buf, size = [], 0
        buf.append(para)
        size += len(para) + 1
    if buf:
        blocks.append("\n".join(buf))
    return blocks or [text[:limit]]


class KnowledgeRetriever:
    """玩家文档检索器（进程内单例即可，线程安全懒加载）。"""

    def __init__(self, docs_dir: Optional[Path] = None):
        self.docs_dir = Path(docs_dir) if docs_dir else DOCS_DIR
        self._lock = threading.Lock()
        self._chunks: List[Chunk] = []
        self._df: Dict[str, int] = {}
        self._avgdl: float = 1.0
        self._item_index: Dict[str, List[int]] = {}
        self._command_vocab: Dict[str, int] = {}
        self._mtimes: Dict[str, float] = {}
        self._meta: Dict[str, Dict[str, str]] = {}
        self._core_text: str = ""
        self._newbie_text: str = ""
        self._built = False

    # ---------- 构建 ----------

    def _fingerprint(self) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for name, _, _, _ in DOC_SPECS:
            path = self.docs_dir / name
            try:
                out[name] = path.stat().st_mtime if path.exists() else 0.0
            except OSError:
                out[name] = 0.0
        return out

    def ensure_ready(self, force: bool = False) -> bool:
        """懒建索引；文档 mtime 变化时自动重建。返回是否可用。"""
        with self._lock:
            fingerprint = self._fingerprint()
            if self._built and not force and fingerprint == self._mtimes:
                return bool(self._chunks)
            self._mtimes = fingerprint
            self._build()
            return bool(self._chunks)

    def _build(self) -> None:
        chunks: List[Chunk] = []
        meta_all: Dict[str, Dict[str, str]] = {}
        for name, weight, kind, label in DOC_SPECS:
            path = self.docs_dir / name
            if not path.exists():
                logger.warning(f"【AI向导】知识库文档缺失：{path.name}")
                continue
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError as e:
                logger.warning(f"【AI向导】知识库文档读取失败 {path.name}: {e}")
                continue
            meta, body = _split_front_matter(raw)
            meta_all[name] = meta
            if kind == "item":
                chunks.extend(self._chunk_item_doc(name, label, weight, body))
            else:
                chunks.extend(self._chunk_heading_doc(name, label, weight, body))

        self._meta = meta_all
        self._chunks = chunks
        self._item_index = {}
        for idx, chunk in enumerate(chunks):
            if chunk.item_name:
                self._item_index.setdefault(_normalize(chunk.item_name), []).append(idx)

        df: Dict[str, int] = {}
        total_dl = 0
        for chunk in chunks:
            total_dl += chunk.dl
            for tok in chunk.tf:
                df[tok] = df.get(tok, 0) + 1
        self._df = df
        self._avgdl = (total_dl / len(chunks)) if chunks else 1.0

        vocab: Dict[str, int] = {}
        for chunk in chunks:
            for cmd in chunk.commands:
                vocab[cmd] = vocab.get(cmd, 0) + 1
        self._command_vocab = vocab

        self._core_text = self._compose_core()
        self._newbie_text = self._compose_newbie()
        self._built = True
        logger.info(
            f"【AI向导】知识库索引就绪：{len(chunks)} 块 / "
            f"{len(self._item_index)} 个物品条目 / 词表 {len(df)}"
        )

    def _chunk_heading_doc(self, name: str, label: str, weight: float, body: str) -> List[Chunk]:
        out: List[Chunk] = []
        path_titles: List[str] = []
        buf: List[str] = []
        seq = 0

        def flush() -> None:
            nonlocal seq
            if not buf:
                return
            title = " · ".join(path_titles) or label
            text = "\n".join(buf).strip()
            buf.clear()
            if not text:
                return
            admin = _is_admin_title(title)
            for piece in _split_long(title, text, MAX_CHUNK_CHARS):
                seq += 1
                out.append(self._make_chunk(name, label, weight, title, piece, seq, admin_only=admin))

        for line in body.splitlines():
            match = _HEADING_RE.match(line)
            if match:
                flush()
                level = len(match.group(1))
                title = match.group(2).strip()
                del path_titles[level - 2 :]
                path_titles.append(title)
                continue
            buf.append(line)
        flush()
        return out

    def _chunk_item_doc(self, name: str, label: str, weight: float, body: str) -> List[Chunk]:
        """物品图鉴：条目行单独成块（只索引名字与标题），说明性段落按标题成块。"""
        out: List[Chunk] = []
        path_titles: List[str] = []
        buf: List[str] = []
        seq = 0

        def flush() -> None:
            nonlocal seq
            if not buf:
                return
            title = " · ".join(path_titles) or label
            text = "\n".join(buf).strip()
            buf.clear()
            if not text:
                return
            for piece in _split_long(title, text, MAX_CHUNK_CHARS):
                seq += 1
                out.append(self._make_chunk(name, label, weight, title, piece, seq))

        for line in body.splitlines():
            match = _HEADING_RE.match(line)
            if match:
                flush()
                level = len(match.group(1))
                title = match.group(2).strip()
                del path_titles[level - 2 :]
                path_titles.append(title)
                continue
            item = _ITEM_LINE_RE.match(line.strip())
            if item:
                flush()
                seq += 1
                item_name = item.group("name").strip()
                title = " · ".join(path_titles + [item_name])
                text = line.strip()[:MAX_ITEM_CHUNK_CHARS]
                chunk = self._make_chunk(name, label, weight, title, text, seq, kind="item")
                chunk.item_name = item_name
                # 物品条目只对「名字 + 标题」建索引：正文是模板化描述，进索引只会稀释打分
                chunk.tf = _tf_map(_bigrams(item_name) + _bigrams(" · ".join(path_titles)))
                chunk.dl = sum(chunk.tf.values()) or 1
                out.append(chunk)
                continue
            buf.append(line)
        flush()
        return out

    def _make_chunk(
        self,
        name: str,
        label: str,
        weight: float,
        title: str,
        text: str,
        seq: int,
        kind: str = "heading",
        admin_only: bool = False,
    ) -> Chunk:
        chunk = Chunk(
            chunk_id=f"{name}#{seq}",
            doc=name,
            label=label,
            title=title,
            text=text,
            kind=kind,
            admin_only=admin_only,
            weight=weight,
        )
        chunk.norm_text = _normalize(text)
        chunk.norm_title = _normalize(title)
        chunk.tf = _tf_map(_bigrams(title) + _bigrams(text))
        chunk.dl = sum(chunk.tf.values()) or 1
        chunk.commands = tuple({m.group(1) for m in _COMMAND_RE.finditer(text)})
        return chunk

    # ---------- 固定注入内容 ----------

    def _compose_core(self) -> str:
        parts: List[str] = []
        for chunk in self._chunks:
            if chunk.doc != CORE_DOC or chunk.admin_only:
                continue
            head = chunk.title.split(" · ")[0]
            if any(head == sec or head.startswith(sec) for sec in CORE_SECTIONS):
                parts.append(f"## {chunk.title}\n{chunk.text}")
        return "\n\n".join(parts)

    def _compose_newbie(self) -> str:
        parts: List[str] = []
        for chunk in self._chunks:
            if chunk.doc != NEWBIE_DOC:
                continue
            parts.append(f"## {chunk.title}\n{chunk.text}")
        return "\n\n".join(parts)

    def core_manual(self, max_chars: int = 7000) -> str:
        """固定核心手册：术语 + 快速上手 + 职业 + 核心数值 + 修炼状态 + FAQ。"""
        if not self.ensure_ready():
            return ""
        return self._core_text[:max_chars]

    def newbie_guide(self, max_chars: int = 6000) -> str:
        """新手引导全文（新玩家整篇注入）。"""
        if not self.ensure_ready():
            return ""
        return self._newbie_text[:max_chars]

    # ---------- 检索 ----------

    def expand_query(self, query: str) -> List[str]:
        """把玩家口语展开成同义词，提升稀疏检索召回。"""
        norm = _normalize(query)
        extra: List[str] = []
        lowered = str(query or "").lower()
        for word, targets in SYNONYMS.items():
            if word in lowered or _normalize(word) in norm:
                extra.extend(targets)
        for alias, canonical in self._alias_map().items():
            if alias and alias in str(query or ""):
                extra.append(canonical.lstrip("/"))
        return extra

    @staticmethod
    def _alias_map() -> Dict[str, str]:
        try:
            from ..webchat_room.commands import ALIAS_MAP

            return dict(ALIAS_MAP)
        except Exception:
            return {}

    def _idf(self, token: str) -> float:
        n = len(self._chunks) or 1
        df = self._df.get(token, 0)
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def _score_chunk(
        self,
        chunk: Chunk,
        query_tokens: Sequence[str],
        query_norm: str,
        commands: Sequence[str],
        items: Sequence[str],
    ) -> float:
        k1, b = 1.5, 0.75
        score = 0.0
        dl = chunk.dl or 1
        for tok in query_tokens:
            tf = chunk.tf.get(tok)
            if not tf:
                continue
            denom = tf + k1 * (1 - b + b * dl / (self._avgdl or 1.0))
            score += self._idf(tok) * (tf * (k1 + 1)) / denom
        if score <= 0:
            return 0.0
        score *= chunk.weight
        # 标题命中：问题里的词出现在小节标题上，几乎就是正确答案所在
        if query_norm and len(query_norm) >= 2 and query_norm in chunk.norm_title:
            score *= 1.8
        elif chunk.norm_title and any(t in chunk.norm_title for t in query_tokens if len(t) > 1):
            score *= 1.35
        # 整句包含
        if query_norm and len(query_norm) >= 4 and query_norm in chunk.norm_text:
            score *= 1.25
        # 指令名命中
        if commands:
            hit = sum(1 for c in commands if c in chunk.commands or c in chunk.text)
            if hit:
                score += 1.5 * min(hit, 3)
        # 物品名精确命中
        if items and chunk.item_name:
            for item in items:
                if item and (item == chunk.item_name or item in chunk.item_name):
                    score += 6.0
                    break
        return score

    def _extract_entities(self, query: str) -> Tuple[List[str], List[str]]:
        """从问题里抽出指令名与物品名，用于精确加权。"""
        commands: List[str] = []
        raw = str(query or "")
        for match in _COMMAND_RE.finditer(raw):
            commands.append(match.group(1))
        vocab = self._command_vocab
        for cmd in vocab:
            if len(cmd) >= 2 and cmd in raw and cmd not in commands:
                commands.append(cmd)
        items: List[str] = []
        norm = _normalize(raw)
        for item_norm in self._item_index:
            if len(item_norm) >= 2 and item_norm in norm:
                items.append(item_norm)
        items.sort(key=len, reverse=True)
        return commands[:8], items[:5]

    def search(
        self,
        query: str,
        top_k: int = 5,
        max_chars: int = 6000,
        include_admin: bool = False,
    ) -> List[ChunkHit]:
        return self.multi_search([query], top_k=top_k, max_chars=max_chars, include_admin=include_admin)

    def multi_search(
        self,
        queries: Sequence[str],
        top_k: int = 5,
        max_chars: int = 6000,
        include_admin: bool = False,
    ) -> List[ChunkHit]:
        """多路检索合并：同块被多个查询命中时加权，避免深度模式重复取同一段。"""
        if not self.ensure_ready():
            return []
        clean_queries = [q for q in queries if str(q or "").strip()]
        if not clean_queries:
            return []

        expanded: List[str] = []
        for q in clean_queries:
            expanded.append(q)
            expanded.extend(self.expand_query(q))

        best: Dict[str, ChunkHit] = {}
        hits_count: Dict[str, int] = {}
        for q in expanded:
            q_norm = _normalize(q)
            tokens = list(dict.fromkeys(_bigrams(q)))
            if not tokens:
                continue
            commands, items = self._extract_entities(q)
            for chunk in self._chunks:
                if chunk.admin_only and not include_admin:
                    continue
                score = self._score_chunk(chunk, tokens, q_norm, commands, items)
                if score <= 0:
                    continue
                hits_count[chunk.chunk_id] = hits_count.get(chunk.chunk_id, 0) + 1
                prev = best.get(chunk.chunk_id)
                if prev is None or score > prev.score:
                    best[chunk.chunk_id] = ChunkHit(chunk=chunk, score=score)

        for chunk_id, hit in best.items():
            times = hits_count.get(chunk_id, 1)
            if times > 1:
                hit.matched_queries = times
                hit.score *= 1 + 0.25 * (times - 1)

        ranked = sorted(best.values(), key=lambda h: h.score, reverse=True)
        out: List[ChunkHit] = []
        used = 0
        for hit in ranked:
            if len(out) >= top_k:
                break
            text = hit.chunk.text
            if used + len(text) > max_chars and out:
                continue
            out.append(hit)
            used += len(text)
        return out

    def match_items(self, query: str, limit: int = 3, min_len: int = 3) -> List[str]:
        """从问题里认出可能的物品名，返回图鉴里的真实条目名。

        玩家问「破境丹」，图鉴条目名却是「地品 · 四品破境增益丹」，
        精确/子串都对不上。这里按「最长公共 n-gram」打分：
        从 6 字到 3 字逐档找，命中越长说明越可能是同一件东西。
        2054 个条目 × 4 档 ≈ 6 万次集合查找，单次问答可忽略。
        """
        if not self.ensure_ready():
            return []
        norm_query = _normalize(query)
        if len(norm_query) < min_len:
            return []
        grams: Dict[int, set] = {
            length: {norm_query[i : i + length] for i in range(len(norm_query) - length + 1)}
            for length in range(6, min_len - 1, -1)
        }
        scored: List[Tuple[int, str]] = []
        for item_norm in self._item_index:
            if len(item_norm) < min_len:
                continue
            best = 0
            for length in range(6, min_len - 1, -1):
                if length > len(item_norm):
                    continue
                window = {item_norm[i : i + length] for i in range(len(item_norm) - length + 1)}
                if window & grams.get(length, ()):  # 有公共 n-gram 就算命中
                    best = length
                    break
            if best:
                scored.append((best, item_norm))
        if not scored:
            return []
        scored.sort(key=lambda x: (-x[0], len(x[1])))
        out: List[str] = []
        for _, item_norm in scored:
            for idx in self._item_index[item_norm][:1]:
                name = self._chunks[idx].item_name
                if name and name not in out:
                    out.append(name)
            if len(out) >= limit:
                break
        return out

    def lookup_items(self, names: Sequence[str], limit: int = 3) -> List[ChunkHit]:
        """按物品名取条目（问答里提到具体丹药/装备时用）。

        精确不中时按子串兜底：玩家问「破境丹」，图鉴条目名却是
        「地品 · 四品破境增益丹」，只有子串匹配才捞得回来。
        """
        if not self.ensure_ready() or not names:
            return []
        out: List[ChunkHit] = []
        seen: set = set()
        for name in names:
            target = _normalize(name)
            if len(target) < 2:
                continue
            idxs = list(self._item_index.get(target) or [])
            if not idxs:
                idxs = [
                    idx
                    for key, ids in self._item_index.items()
                    if target in key or key in target
                    for idx in ids
                ]
                # 名字长度越接近，越可能是玩家真正在问的那件
                idxs.sort(key=lambda i: abs(len(self._chunks[i].item_name) - len(target)))
            for idx in idxs[:limit]:
                chunk = self._chunks[idx]
                if chunk.chunk_id in seen:
                    continue
                seen.add(chunk.chunk_id)
                out.append(ChunkHit(chunk=chunk, score=99.0))
            if len(out) >= limit * 2:
                break
        return out

    def best_score(self, hits: Sequence[ChunkHit]) -> float:
        return max((h.score for h in hits), default=0.0)

    # ---------- 版本新鲜度 ----------

    def version_report(self, expected: str = "") -> List[Tuple[str, str, bool]]:
        """返回 (文档, front matter 版本, 是否与期望一致)。"""
        self.ensure_ready()
        out: List[Tuple[str, str, bool]] = []
        for name, _, _, label in DOC_SPECS:
            meta = self._meta.get(name) or {}
            version = meta.get("game_version", "")
            out.append((label, version, bool(expected and version == expected)))
        return out

    def stale_docs(self, expected: str) -> List[str]:
        if not expected:
            return []
        return [
            label
            for label, version, ok in self.version_report(expected)
            if version and not ok
        ]

    def stats(self) -> Dict[str, Any]:
        self.ensure_ready()
        return {
            "chunks": len(self._chunks),
            "items": len(self._item_index),
            "vocab": len(self._df),
            "core_chars": len(self._core_text),
            "newbie_chars": len(self._newbie_text),
            "docs": [name for name, _, _, _ in DOC_SPECS if (self.docs_dir / name).exists()],
        }


_DEFAULT: Optional[KnowledgeRetriever] = None
_DEFAULT_LOCK = threading.Lock()


def get_retriever() -> KnowledgeRetriever:
    """进程内共享的检索器实例。"""
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = KnowledgeRetriever()
        return _DEFAULT


def format_hits(hits: Sequence[ChunkHit], max_chars: int = 6000) -> str:
    """把命中块拼成提示词里的资料段，带出处，便于模型引用与自检。"""
    parts: List[str] = []
    used = 0
    for idx, hit in enumerate(hits, start=1):
        text = hit.chunk.text.strip()
        header = f"[{idx}] {hit.chunk.citation}"
        block = f"{header}\n{text}"
        if used + len(block) > max_chars and parts:
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts)
