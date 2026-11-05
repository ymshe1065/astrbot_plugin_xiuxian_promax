# -*- coding: utf-8 -*-
"""QQ 文本方言解析：把插件产出的长文本回复还原成结构化数据。

插件的文本回复是为 QQ 纯文本排版的，形态高度规整：
     📊 境界排行榜
     ━━━━━━━━━━━━━━━
     【青云秘境】(ID:1)
       等级要求：无限制
       修为奖励：500-1,500
     💡 使用 /探索秘境 <ID> 进入（如：/探索秘境 1）

网页端不必原样显示这坨文字，解析成「列表 / 键值卡」后原生排版更好读。
短消息（<5 行且无结构）返回 None，仍按普通聊天气泡展示。
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

# 分隔线：━━ ── ══ ---- ==== 等
_DIVIDER = re.compile(r"^[\s\-─━═—=_~·*]{5,}$")
# 记录头：【青云秘境】(ID:1) / [万妖洞]
_RECORD = re.compile(r"^[【\[]([^】\]]{1,30})[】\]]\s*(.*)$")
# 榜单行：1. 道友名 / 2、某某
_RANKED = re.compile(r"^([1-9]\d?)\s*[.、．]\s*(\S.{0,40})$")
# 键值：标签：值（标签不含空白/括号/标点，长度受限）
_KV = re.compile(r"^([^\s：:，,。、（）()\[\]【】]{1,18})\s*[：:]\s*(.+)$")
# 一行内的多字段分隔：境界：炼气期一层 | 修为：0/500
_KV_SPLIT = re.compile(r"\s*[|｜]\s*")
# 提示行：💡 📌 ✨
_TIP = re.compile(r"^([💡📌✨⭐🔔📢])\ufe0f?\s*(.+)$")
# 文本内出现的指令
_CMD = re.compile(r"(/[^\s，。、；：）)】\]|]+)")
# 序号列表项：1. 剑修 / 1、剑修 / ①剑修
_OPTION = re.compile(r"^\s*(?:[（(]?([1-9])\s*[.、．)）]|[①②③④⑤⑥⑦⑧⑨])\s*(.{1,24})$")
# 引导模板：发送：我要修仙 职业名
_SEND_TMPL = re.compile(r"发送[:：]\s*([^\n（(】]{1,24})")

_MAX_TEXT = 20000
_MAX_ACTIONS = 12
_OPTION_SYMBOLS = "①②③④⑤⑥⑦⑧⑨"


def _is_divider(line: str) -> bool:
    s = line.strip()
    return bool(s) and bool(_DIVIDER.match(s))


def _split_title(lines: list[str]) -> tuple[str, int]:
    """首行若不是分隔线，作为标题；返回 (标题, 正文起始索引)。"""
    for i, line in enumerate(lines[:3]):
        s = line.strip()
        if not s or _is_divider(s):
            continue
        if len(s) <= 40 and not _RECORD.match(s) and not _KV.match(s):
            return s, i + 1
        break
    return "", 0


def _parse_kv_pairs(line: str) -> list[list[str]]:
    """解析一行里的一个或多个键值对（支持 甲：1 | 乙：2）。"""
    parts = [p for p in _KV_SPLIT.split(line) if p.strip()]
    pairs: list[list[str]] = []
    for part in parts:
        m = _KV.match(part.strip())
        if not m:
            return []
        label, value = m.group(1).strip(), m.group(2).strip()
        if not value:
            return []
        pairs.append([label, value])
    return pairs


def _valid_cmd(cmd: str) -> bool:
    """过滤掉 0/500 这类被误认成指令的分数。"""
    body = cmd.lstrip("/").strip()
    # 插件里没有单字指令，「时间/时」这种是被斜杠切出来的残渣
    if len(body) < 2 or len(body) > 30:
        return False
    if re.fullmatch(r"[\d\s.,:%+\-/]+", body):
        return False
    return bool(re.search(r"[\u4e00-\u9fffA-Za-z]", body))


def _extract_actions(text: str) -> list[dict[str, str]]:
    """从文本中提取可点击指令。带「如：/xxx 1」的示例优先取具体参数。"""
    actions: list[dict[str, str]] = []
    seen: set[str] = set()
    # 优先：括号里的示例，如 （如：/探索秘境 1）
    examples = re.findall(r"[（(]\s*如[：:]\s*(/[^）)]+)[）)]", text)
    for ex in examples:
        cmd = ex.strip()
        if cmd and cmd not in seen:
            seen.add(cmd)
            actions.append({"label": _action_label(cmd), "cmd": cmd})
    for raw in _CMD.findall(text):
        cmd = raw.rstrip("，。、；:：")
        # 模板占位符（<ID>、xxx）与分数不是可直接发送的指令
        if not _valid_cmd(cmd) or cmd in seen or "<" in cmd or ">" in cmd:
            continue
        seen.add(cmd)
        actions.append({"label": _action_label(cmd), "cmd": cmd})
        if len(actions) >= _MAX_ACTIONS:
            break
    return actions[: _MAX_ACTIONS]


def _action_label(cmd: str) -> str:
    body = cmd.lstrip("/")
    return body or cmd


# ---------- 仙市行情 ----------
# 「 仙市 · 第 3 盘 · 午市 」 / 「 仙市 · 已收市 」
_MARKET_HEAD = re.compile(r"仙市\s*[·・]\s*(?:第\s*(\d+)\s*盘|已收市)")
# S01 丹阁        12.34   ▲1.20%   ▲3.40%   ▲0.80%
_MARKET_ROW = re.compile(r"^([SF])(\d{2,3})\s+(.+)$")
_MARKET_PCT = re.compile(r"^(?:[▲▼]\s*\d+(?:\.\d+)?%|—+)$")
_MARKET_PRICE = re.compile(r"\d[\d,]*\.\d+")
_MARKET_COLS = re.compile(r"[（(]\s*([^（）()]*?/[^（）()]*?)\s*[）)]")
_MARKET_HINT = re.compile(r"^(当盘风声|下盘风声)(?:\s*[·・]\s*(\d{1,2}:\d{2}))?$")
# 一天 16 盘（07:00–22:00），用来画盘中进度
_MARKET_TICKS = 16


def _market_row(line: str, group: str) -> dict[str, Any] | None:
    """解析一行行情：代码 / 名称 / 价格 / 一排涨跌幅 / 风险星。"""
    m = _MARKET_ROW.match(line)
    if not m:
        return None
    rest = m.group(3)
    stars = rest.count("★")
    parts = rest.replace("★", " ").split()
    pcts: list[str] = []
    while parts and _MARKET_PCT.match(parts[-1]):
        pcts.insert(0, parts.pop())
    if not parts:
        return None
    price = ""
    if _MARKET_PRICE.fullmatch(parts[-1]):
        price = parts.pop()
    name = " ".join(parts).strip()
    if not price:
        # 名称顶满列宽时 `_pad` 不再补空格，价格会粘在名字后面
        mm = re.match(r"^(.*?)(\d[\d,]*\.\d+)$", name)
        if mm:
            name, price = mm.group(1).strip(), mm.group(2)
    if not name or not price:
        return None
    return {
        "code": f"{m.group(1)}{m.group(2)}",
        "name": name,
        "kind": "fund" if m.group(1) == "F" else "stock",
        "group": group,
        "price": price,
        "pcts": pcts,
        "risk": stars,
    }


def _parse_market_lines(lines: list[str], text: str) -> dict[str, Any] | None:
    """把 `/仙市` 面板还原成「风声 + 行情表」。

    文本是给 QQ 用空格拼出来的伪表格，换行即错位，网页端不必照抄：
    拆成行数据后由前端按真表格排版，顺便带上分组与列名。
    """
    head: re.Match[str] | None = None
    head_idx = 0
    for i, ln in enumerate(lines[:4]):
        found = _MARKET_HEAD.search(ln)
        if found:
            head, head_idx = found, i
            break
    if head is None:
        return None

    tick = int(head.group(1)) if head.group(1) else 0
    closed = head.group(1) is None
    title = lines[head_idx].strip().strip("「」【】 ").strip() or "仙市"
    head_line = lines[head_idx].strip()

    hints: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    stock_cols: list[str] = []
    status = ""
    group = ""
    cur_hint: dict[str, Any] | None = None

    for ln in lines:
        s = ln.strip()
        if not s or _is_divider(s) or s == head_line:
            continue
        if s in ("股票", "基金"):
            group = "stock" if s == "股票" else "fund"
            cur_hint = None
            continue
        hm = _MARKET_HINT.match(s)
        if hm:
            cur_hint = {"label": hm.group(1), "hour": hm.group(2) or "", "title": "", "detail": ""}
            hints.append(cur_hint)
            continue
        row = _market_row(s, group)
        if row is not None:
            rows.append(row)
            cur_hint = None
            continue
        cm = _MARKET_COLS.search(s)
        if cm and not stock_cols:
            stock_cols = [c.strip() for c in re.split(r"[/／]", cm.group(1)) if c.strip()]
            continue
        if any(k in s for k in ("距收市", "距开盘", "闭市中")):
            status = re.sub(r"\s+", " ", s.replace("\u3000", " "))
            continue
        if s.startswith("指令"):
            continue
        if cur_hint is not None:
            if not cur_hint["title"]:
                cur_hint["title"] = s.lstrip("·• ").strip()
            elif not cur_hint["detail"]:
                cur_hint["detail"] = s.strip()
            continue

    if not rows:
        return None

    digest = hashlib.md5(text.encode("utf-8", "ignore")).hexdigest()[:12]
    return {
        "id": f"mkt_{digest}",
        "kind": "market",
        "title": title,
        "subtitle": status,
        "footer": "",
        "sections": [],
        "items": [],
        "actions": _extract_actions(text),
        "market": {
            "tick": tick,
            "closed": closed,
            "progress": 0.0 if closed else min(1.0, tick / _MARKET_TICKS),
            "hints": hints,
            "rows": rows,
            "stock_cols": stock_cols or ["本盘", "今日", "近5盘"],
            "fund_cols": ["本盘", "今日"],
        },
    }


def parse_market(raw: str) -> dict[str, Any] | None:
    """单独入口（测试与外部调用）；不是仙市面板时返回 None。"""
    if not raw or not isinstance(raw, str) or "仙市" not in raw:
        return None
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    if len(text) > _MAX_TEXT:
        return None
    return _parse_market_lines([ln.rstrip() for ln in text.split("\n")], text)


def parse_text(raw: str) -> dict[str, Any] | None:
    """解析长文本回复；不值得结构化时返回 None。"""
    if not raw or not isinstance(raw, str):
        return None
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    if len(text) > _MAX_TEXT:
        return None
    # 链接/代码等不要乱解析
    if "```" in text:
        return None

    lines = [ln.rstrip() for ln in text.split("\n")]
    while lines and not lines[-1].strip():
        lines.pop()
    # 仙市面板优先：空格拼出来的伪表格交给前端按真表格排版
    market = _parse_market_lines(lines, text)
    if market is not None:
        return market
    # 短消息（寒暄/动作反馈）不结构化，仍按气泡显示
    if len(lines) < 3:
        return None

    title, start = _split_title(lines)
    sections: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    footer = ""
    has_divider = False
    kv_count = 0

    cur: dict[str, Any] | None = None          # 当前键值分区
    cur_rec: dict[str, Any] | None = None      # 当前记录

    def ensure_section(name: str = "") -> dict[str, Any]:
        nonlocal cur
        if cur is None:
            cur = {"name": name, "rows": [], "lines": []}
            sections.append(cur)
        elif name and not cur["name"]:
            cur["name"] = name
        return cur

    for line in lines[start:]:
        s = line.strip()
        if not s:
            cur_rec = None
            continue
        if _is_divider(s):
            has_divider = True
            cur = None
            cur_rec = None
            continue

        tip = _TIP.match(s)
        if tip:
            footer = (footer + " " + tip.group(2).strip()).strip()
            cur_rec = None
            continue

        rec = _RECORD.match(s)
        if rec:
            cur_rec = {"title": rec.group(1).strip(), "meta": rec.group(2).strip(), "rows": []}
            records.append(cur_rec)
            continue

        ranked = _RANKED.match(s)
        if ranked:
            cur_rec = {
                "title": ranked.group(2).strip(),
                "meta": f"#{ranked.group(1)}",
                "rows": [],
                "lines": [],
            }
            records.append(cur_rec)
            continue

        pairs = _parse_kv_pairs(s)
        if pairs:
            if cur_rec is not None:
                cur_rec["rows"].extend(pairs)
            else:
                ensure_section()["rows"].extend(pairs)
            kv_count += len(pairs)
            continue

        # 保留行首缩进（「　正文」是上一行的补充说明，缩进本身有语义）
        body = line.rstrip()
        if cur_rec is not None:
            cur_rec.setdefault("lines", []).append(body)
        else:
            ensure_section()["lines"].append(body)

    # 丢掉全空的分区
    sections = [s for s in sections if s["rows"] or s["lines"]]
    records = [r for r in records if r["rows"] or r.get("lines") or r["meta"] or r["title"]]
    for rec in records:
        rec.setdefault("lines", [])

    structured = has_divider or bool(records) or kv_count >= 3
    if not structured:
        return None
    if not sections and not records:
        return None

    actions = _extract_actions(text)
    kind = "list" if records and not sections else "card"
    if records and sections:
        kind = "mixed"

    digest = hashlib.md5(text.encode("utf-8", "ignore")).hexdigest()[:12]
    return {
        "id": f"txt_{digest}",
        "kind": kind,
        "title": title,
        "subtitle": "",
        "footer": footer,
        "sections": sections,
        "items": records,
        "actions": actions,
    }


# ---------- 战斗战报 ----------
_BATTLE_START = re.compile(r"(?:☆[━─═\-]*\s*)?(?:Boss战|战斗|决斗|切磋)开始")
_BATTLE_END = re.compile(r"(?:☆[━─═\-]*\s*)?(?:战斗|决斗|切磋)结束")
_BATTLE_ROUND = re.compile(r"^\s*--\s*第\s*(\d+)\s*回合\s*--\s*$")
_BATTLE_WINNER = re.compile(r"^\s*(?:胜者|获胜)[：:]\s*(.+?)\s*$")
_BATTLE_TROPHY = re.compile(r"🏆\s*(?:决斗|切磋)?结束[，,]?\s*(.+?)\s*(?:获胜|胜出|赢)")
_BATTLE_DRAW = re.compile(r"🏆\s*(?:决斗|切磋)?结束[，,]?\s*平局")
_BATTLE_ROUNDS = re.compile(r"^\s*(?:战斗回合|回合数)[：:]\s*(\d+)\s*$")


def parse_battle(raw: str) -> dict[str, Any] | None:
    """把逐回合战报解析成「摘要 + 可展开全文」的卡片。

    网页聊天室里战报通常很长，默认只露胜负与回合数，点开再看逐回合详情。
    不是战报（没有开场标记或回合标记）时返回 None，交给普通文本解析。
    """
    if not raw or not isinstance(raw, str):
        return None
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    if len(text) > _MAX_TEXT:
        return None

    lines = [ln.rstrip() for ln in text.split("\n")]
    markers = [ln for ln in lines if _BATTLE_ROUND.match(ln)]
    has_start = any(_BATTLE_START.search(ln) for ln in lines)
    has_end = any(_BATTLE_END.search(ln) for ln in lines)
    # 世界 Boss 的结果卡会先列摘要再附「📜 战斗详情」，没有标准开场行
    has_result = any("战斗结果" in ln for ln in lines)
    if not (markers and (has_start or has_end or has_result)):
        return None

    is_boss = any(k in text for k in ("Boss", "BOSS", "boss"))
    title = "Boss 战报" if is_boss else "战斗战报"

    subtitle = ""
    for ln in lines:
        s = ln.strip()
        if s.startswith(("💡", "📜", "📌")):
            continue
        if " VS " in s or ("挑战" in s and "结果" not in s):
            subtitle = s.strip("☆━─═- ")
            break

    winner = ""
    draw = False
    result = ""
    damage = ""
    boss_hp = ""
    player_hp = ""
    rounds = 0
    remain = ""
    for ln in lines:
        s = ln.strip()
        m = _BATTLE_WINNER.match(s)
        if m and not winner:
            winner = m.group(1).strip()
        m = _BATTLE_TROPHY.search(s)
        if m and not winner:
            winner = m.group(1).strip()
        if not draw and _BATTLE_DRAW.search(s):
            draw = True
        m = _BATTLE_ROUNDS.match(s)
        if m:
            rounds = int(m.group(1))
        if s.startswith(("结果：", "结果:")):
            result = s
        elif s.startswith(("造成伤害：", "造成伤害:")):
            damage = s
        elif s.startswith(("Boss剩余：", "Boss剩余:")):
            boss_hp = s
        elif s.startswith(("你的剩余气血：", "你的剩余气血:")):
            player_hp = s
        elif "剩余 HP" in s or "剩余血量" in s:
            # 取最后一条，通常是战斗结束时的血量
            remain = s
    if not rounds and markers:
        try:
            rounds = max(int(_BATTLE_ROUND.match(m).group(1)) for m in markers)
        except ValueError:
            rounds = len(markers)

    bits: list[str] = []
    if winner:
        bits.append(f"胜者：{winner}")
    elif draw:
        bits.append("平局")
    elif result:
        bits.append(result)
    if rounds:
        bits.append(f"{rounds} 回合")
    if damage:
        bits.append(damage)
    if boss_hp:
        bits.append(boss_hp)
    if player_hp:
        bits.append(player_hp)
    if remain and not (result or damage or boss_hp or player_hp):
        bits.append(remain.replace("|", " · "))

    digest = hashlib.md5(text.encode("utf-8", "ignore")).hexdigest()[:12]
    return {
        "id": f"battle_{digest}",
        "kind": "battle",
        "title": title,
        "subtitle": subtitle,
        "summary": " · ".join(bits) or "战斗已结束",
        "detail": text,
        "detail_hidden": False,
        "folded": True,
        "sections": [],
        "items": [],
        "actions": [],
    }


def _normalize_cmd(cmd: str) -> str:
    """选项最终都要当指令发出，补齐唤醒前缀。"""
    cmd = cmd.strip()
    if not cmd:
        return cmd
    return cmd if cmd.startswith("/") else f"/{cmd}"


def extract_options(raw: str) -> list[dict[str, str]]:
    """从「请选择 1.剑修 2.体修」这类回复里提取可点选项。

    返回 [{"label": "剑修", "cmd": "/我要修仙 剑修"}]，供前端渲染成按钮。
    同时支持记录式引导：
        请选择你的修炼方式（发送：我要修仙 职业名）
        【灵修】定位：均衡法系
    """
    if not raw or not isinstance(raw, str):
        return []
    text = raw.replace("\r\n", "\n")
    tm = _SEND_TMPL.search(text)
    base = ""
    if tm:
        parts = tm.group(1).strip().split()
        # 模板末段多为占位符（职业名/宗门名），拼具体选项时要去掉
        base = " ".join(parts[:-1]) if len(parts) > 1 else ""

    # ① 序号式：请发送：选择灵根 1/2/3
    seq_tm = re.search(r"发送[:：]\s*([^\s0-9]{1,14})\s*1\s*/\s*2", text)
    if seq_tm:
        base_cmd = seq_tm.group(1).strip()
        opts: list[dict[str, str]] = []
        for line in text.split("\n"):
            m = re.match(r"^\s*([1-9])\s*[.、．]\s*(.+)$", line)
            if not m:
                continue
            num, rest = m.group(1), m.group(2).strip()
            lm = re.match(r"^【([^】]{1,14})】", rest)
            label = lm.group(1) if lm else re.sub(r"[（(].*?[）)]$", "", rest)[:12]
            opts.append({"label": label, "cmd": _normalize_cmd(f"{base_cmd} {num}")})
            if len(opts) >= 12:
                break
        if len(opts) >= 2:
            return opts

    # ② 序号式选项
    options: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in text.split("\n"):
        s = line.strip().lstrip("丨|").strip()
        m = _OPTION.match(s)
        if not m:
            continue
        label = (m.group(2) or "").strip(" 　-—")
        label = re.sub(r"[（(].*?[）)]$", "", label).strip()
        if not label or len(label) > 20 or label in seen:
            continue
        seen.add(label)
        cmd = f"{base} {label}".strip() if base else label
        options.append({"label": label, "cmd": _normalize_cmd(cmd)})
        if len(options) >= 12:
            break
    if len(options) >= 2:
        return options

    # ③ 记录式选项：正文给「发送：<命令模板>」，选项写成【名字】
    if not ("请选择" in text or "选择你" in text or "发送：" in text or "发送:" in text):
        return []
    heads = re.findall(r"^[ \t]*【([^】]{1,12})】", text, re.M)
    if len(heads) < 2:
        return []
    out: list[dict[str, str]] = []
    for h in heads[:16]:
        h = h.strip()
        if not h:
            continue
        cmd = f"{base} {h}".strip() if base else h
        out.append({"label": h, "cmd": _normalize_cmd(cmd)})
    return out
