# utils/arg_parse.py
"""指令参数归一化：全角字符、零宽字符与「全部/一键」类关键词的统一定义。

灵田种植、洞天升级、自动代理等多处指令都支持 `all` 这一类批量参数。
此前各处各写一套关键词，导致 `/种植 灵草 所有`、`/关闭代理 灵田 全部格`
这类写法在一处能用、在另一处却提示「无法识别」。这里统一收口。
"""
import re
import unicodedata

__all__ = [
    "ALL_KEYWORDS",
    "PLOT_ALL_KEYWORDS",
    "normalize_arg_text",
    "is_all_keyword",
]

# 通用的「全部」关键词（英文统一小写比较）
ALL_KEYWORDS = frozenset({
    "all", "auto", "max",
    "全部", "全满", "全种", "所有", "满", "满格", "满级",
    "种满", "一键", "一键种满", "全部种满",
})

# 格号选择场景额外接受 `*` 与展示文案「全部格/所有格」
PLOT_ALL_KEYWORDS = ALL_KEYWORDS | frozenset({"*", "全部格", "所有格", "全格"})

# 首尾的装饰性标点（玩家常带句号、感叹号）
_TAIL_PUNCT = "。．.!！?？,，;；、~～"

_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\ufeff]")
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


def normalize_arg_text(raw: str) -> str:
    """归一化玩家输入的参数文本。

    - 全角数字/空格转半角
    - 去掉零宽字符（QQ/微信复制粘贴常见）
    - 去掉首尾标点与多余空白
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    text = _ZERO_WIDTH_RE.sub("", text)
    # NFKC：全角字母/数字/标点转半角（如 ＡＬＬ -> ALL、１-２０ -> 1-20）
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_FULLWIDTH_DIGITS).replace("\u3000", " ")
    text = text.strip().strip(_TAIL_PUNCT).strip()
    return text


def is_all_keyword(text: str, keywords=None) -> bool:
    """判断参数是否是「全部」类关键词。"""
    pool = keywords if keywords is not None else ALL_KEYWORDS
    return normalize_arg_text(text).lower() in pool
