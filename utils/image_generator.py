# utils/image_generator.py
"""信息卡片渲染器 v3 - 国风绢帛·墨金·多字体层次"""
from __future__ import annotations

import os
import re
import uuid
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Optional, Dict, List, Tuple

try:
    from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    from astrbot.api import logger
except ImportError:  # 允许脱离 AstrBot 运行生成脚本/离线测试
    import logging
    logger = logging.getLogger("xiuxian.image_generator")

# ===== 资源路径 =====
RES_DIR = Path(__file__).resolve().parents[1] / "resources"
BG_DIR = RES_DIR / "backgrouds"
FONT_KAI = RES_DIR / "LXGWWenKaiGB.ttf"                 # 文楷（末位兜底，不再主用）
FONT_HEAVY = RES_DIR / "SourceHanSerifSC-Heavy.otf"     # 大标题：最重碑刻
FONT_BOLD = RES_DIR / "SourceHanSerifSC-Bold.otf"       # 区块名：鎏金碑刻
FONT_SEMI = RES_DIR / "SourceHanSerifSC-SemiBold.otf"   # 数值：清晰醒目
FONT_MED = RES_DIR / "SourceHanSerifSC-Medium.otf"      # 字段名/正文
FONT_XINGKAI = RES_DIR / "MaShanZheng-Regular.ttf"      # 行楷：副标题/脚注墨迹
FONT_EMOJI = RES_DIR / "OpenMoji-black-glyf.ttf"        # OpenMoji：装饰符号（程序镀金）
CARD_TEMP_DIR = Path(tempfile.gettempdir()) / "xiuxian_cards"

# emoji 渲染：需要 109px 的 OpenMoji 无所谓，但 Noto 需要固定尺寸；OpenMoji 自由
EMOJI_RENDER_SIZE = 96  # emoji 高清渲染基准，之后缩放

# ===== 国风配色 =====
INK_BG_TOP = (38, 32, 26)
INK_PANEL = (48, 40, 32, 228)
INK_PANEL_EDGE = (198, 161, 91)
INK_GOLD = (232, 196, 122)         # 鎏金标题
INK_GOLD_DIM = (176, 148, 100)     # 暗金副标题
INK_TEXT = (242, 232, 214)         # 宣纸白（数值）
INK_LABEL = (186, 166, 134)        # 淡墨金（字段名）
INK_SECTION = (236, 202, 132)      # 区块名·亮金
INK_ACCENT = (176, 141, 87)
INK_FOOTER = (160, 140, 110)
INK_DIVIDER = (110, 94, 72)
INK_SHADOW = (0, 0, 0, 180)

# ===== 卡片开关 =====
_CARD_FLAGS: Dict[str, bool] = {"__enabled__": True}


def set_card_flags(flags: Dict[str, bool]):
    if isinstance(flags, dict):
        _CARD_FLAGS.update(flags)


def card_enabled(key: str) -> bool:
    if not _CARD_FLAGS.get("__enabled__", True):
        return False
    return _CARD_FLAGS.get(key, True)


def _pick(*paths) -> Optional[Path]:
    for p in paths:
        if p and Path(p).exists():
            return Path(p)
    return None


_font_cache: Dict[Tuple[str, int], ImageFont.FreeTypeFont] = {}


def _load_font(size: int, style: str = "text"):
    """五级字体层次（缺失自动降级，最终兜底文楷/默认）：
    heavy=大标题 / bold=区块 / semi=数值 / text=字段正文 / xing=行楷点缀"""
    key = (style, size)
    if key in _font_cache:
        return _font_cache[key]
    table = {
        "heavy": (FONT_HEAVY, FONT_BOLD, FONT_MED),
        "bold": (FONT_BOLD, FONT_HEAVY, FONT_MED),
        "semi": (FONT_SEMI, FONT_BOLD, FONT_MED),
        "text": (FONT_MED, FONT_SEMI, FONT_KAI),
        "xing": (FONT_XINGKAI, FONT_MED, FONT_KAI),
    }
    f = None
    for cand in table.get(style, (FONT_MED, FONT_KAI)):
        if cand and Path(cand).exists():
            f = ImageFont.truetype(str(cand), size)
            break
    if f is None:
        f = ImageFont.load_default()
    _font_cache[key] = f
    return f


def _cover_resize(img, w: int, h: int):
    sw, sh = img.size
    scale = max(w / sw, h / sh)
    img = img.resize((int(sw * scale) + 1, int(sh * scale) + 1), Image.LANCZOS)
    x, y = (img.size[0] - w) // 2, (img.size[1] - h) // 2
    return img.crop((x, y, x + w, y + h))


def _draw_corner_clouds(draw, w: int, h: int, alpha: int = 30):
    gold = INK_ACCENT + (alpha,)
    r = 46
    for cx, cy, dx, dy in ((56, 56, 1, 1), (w - 56, 56, -1, 1), (56, h - 56, 1, -1), (w - 56, h - 56, -1, -1)):
        draw.arc([cx - r, cy - r, cx + r, cy + r], start=0 if dx > 0 else 90, end=90 if dx > 0 else 180, fill=gold, width=2)
        draw.arc([cx + dx * 20 - 26, cy + dy * 20 - 26, cx + dx * 20 + 26, cy + dy * 20 + 26],
                 start=0 if dx > 0 else 90, end=90 if dx > 0 else 180, fill=gold, width=2)
    cx = w // 2
    draw.polygon([(cx - 8, 30), (cx, 22), (cx + 8, 30), (cx, 38)], outline=gold, width=2)


def _font_textlen(font, text: str) -> float:
    """用字体对象度量文本宽度（无 Draw 时的探针）"""
    if hasattr(font, "getlength"):
        return font.getlength(text)
    return font.getbbox(text)[2]


_XINGKAI_SAFE = re.compile(r'^[\u4e00-\u9fff\u3400-\u4dbf a-zA-Z0-9，。：；！？、「」『』（）()\-\+/%＆%,.:;!?~*×\u3000]*$')


def _xing_font_for(text: str, size: int):
    """行楷字体只用于纯中文/常规标点文本（缺 emoji 字形会渲染豆腐块）；
    含特殊符号时自动回退宋体。"""
    if _XINGKAI_SAFE.match(text or "") and FONT_XINGKAI.exists():
        return ImageFont.truetype(str(FONT_XINGKAI), size)
    return _load_font(size, "semi")


def _trunc(draw, text: str, font, max_width: int) -> str:
    text = str(text)
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text + "…"


def _shadow(draw, xy, text, font, fill):
    x, y = xy
    draw.text((x + 1, y + 2), text, font=font, fill=INK_SHADOW)
    draw.text((x, y), text, font=font, fill=fill)


def _gold_gradient_text(canvas, xy, text, font) -> Tuple[int, int]:
    """烫金渐变文字：上亮金下暗金，带墨色柔影与微光晕。返回 (宽, 高)"""
    x, y = xy
    tw = int(_font_textlen(font, text)) + 8
    th = font.size + 14
    # 文字蒙版
    mask = Image.new("L", (tw, th), 0)
    md = ImageDraw.Draw(mask)
    md.text((4, 4), text, font=font, fill=255)
    bbox = mask.getbbox()
    if not bbox:
        draw.text((x, y), text, font=font, fill=INK_GOLD)
        return tw, th
    mask = mask.crop(bbox)
    w2, h2 = mask.size
    # 竖向鎏金渐变
    grad = Image.new("RGB", (1, h2))
    top, bottom = (248, 218, 148), (176, 132, 62)
    for yy in range(h2):
        t = yy / max(1, h2 - 1)
        grad.putpixel((0, yy), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    grad = grad.resize((w2, h2))
    # 墨影层
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.text((x + 2, y + 3), text, font=font, fill=INK_SHADOW)
    shadow = shadow.filter(ImageFilter.GaussianBlur(2))
    # 金光晕
    glow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.text((x, y), text, font=font, fill=(230, 190, 110, 90))
    glow = glow.filter(ImageFilter.GaussianBlur(6))
    canvas.alpha_composite(glow)
    canvas.alpha_composite(shadow)
    # 贴上渐变金字
    gold_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    gold_layer.paste(grad, (int(x + bbox[0] - 4), int(y + bbox[1] - 4)), mask)
    canvas.alpha_composite(gold_layer)
    return tw, th


_EMOJI_RE = re.compile(
    '['
    '\U0001F300-\U0001FAFF'   # 主要 emoji 区块
    '\U00002600-\U000027BF'   # 杂项符号（⚔✡☯等）
    '\U0001F000-\U0001F02F'
    '\U00002190-\U000021FF'   # 箭头
    '\U00002B00-\U00002BFF'
    ']'
)


def _split_emoji(text: str) -> List[Tuple[str, bool]]:
    """把文本切成 (片段, 是否emoji) 序列"""
    parts = []
    last = 0
    for m in _EMOJI_RE.finditer(text):
        if m.start() > last:
            parts.append((text[last:m.start()], False))
        # emoji 含可选 VS16
        end = m.end()
        if end < len(text) and text[end] == '\uFE0F':
            end += 1
        parts.append((text[m.start():end], True))
        last = end
    if last < len(text):
        parts.append((text[last:], False))
    return [(p, e) for p, e in parts if p]


def _draw_mixed(canvas_draw_image, xy, text, font, emoji_px: int, fill,
                emoji_color=(232, 196, 122), shadow=True):
    """中文+emoji 混排绘制：中文用字体，emoji 用 OpenMoji 镀金位图。
    canvas_draw_image: RGBA Image（用于 emoji 粘贴）；xy 为基线左上角。返回结束 x。"""
    x, y = xy
    d = ImageDraw.Draw(canvas_draw_image)
    emoji_font = None
    if FONT_EMOJI.exists():
        emoji_font = ImageFont.truetype(str(FONT_EMOJI), emoji_px * 3)  # 高清渲染 3x
    for part, is_emoji in _split_emoji(text):
        if is_emoji:
            if emoji_font is None:
                d.text((x, y), part, font=font, fill=fill)
                x += d.textlength(part, font=font)
                continue
            # 高清渲染 emoji 剪影
            ew = int(emoji_px * 1.6)
            eh = int(emoji_px * 1.35)
            tmp = Image.new('L', (ew * 3, eh * 3), 0)
            td = ImageDraw.Draw(tmp)
            try:
                td.text((6, 0), part, font=emoji_font, fill=255)
            except Exception:
                d.text((x, y), part, font=font, fill=fill)
                x += d.textlength(part, font=font)
                continue
            bbox = tmp.getbbox()
            if not bbox:
                d.text((x, y), part, font=font, fill=fill)
                x += d.textlength(part, font=font)
                continue
            tmp = tmp.crop(bbox)
            scale = emoji_px / tmp.height
            tmp = tmp.resize((max(1, int(tmp.width * scale)), emoji_px), Image.LANCZOS)
            # 镀金：剪影作为蒙版填金色渐变 + 柔影
            gold = Image.new('RGBA', canvas_draw_image.size, (0, 0, 0, 0))
            # 竖向金渐变
            grad = Image.new('RGB', (1, tmp.height))
            for yy in range(tmp.height):
                t = yy / max(1, tmp.height - 1)
                grad.putpixel((0, yy), tuple(int(238 + (176 - 238) * t) if i == 0 else
                                             int(206 + (150 - 206) * t) if i == 1 else
                                             int(128 + (74 - 128) * t) for i in range(3)))
            grad = grad.resize((tmp.width, tmp.height))
            glyph = Image.new('RGBA', tmp.size, (0, 0, 0, 0))
            glyph.paste(grad, (0, 0), tmp)
            gx, gy = int(x), int(y + emoji_px * 0.12)
            if shadow:
                sh = Image.new('RGBA', canvas_draw_image.size, (0, 0, 0, 0))
                sh.paste(Image.new('RGBA', tmp.size, (0, 0, 0, 150)), (gx + 1, gy + 2), tmp)
                canvas_draw_image.alpha_composite(sh.filter(ImageFilter.GaussianBlur(1.5)))
            gold_layer = Image.new('RGBA', canvas_draw_image.size, (0, 0, 0, 0))
            gold_layer.paste(glyph, (gx, gy), glyph)
            canvas_draw_image.alpha_composite(gold_layer)
            x += tmp.width + emoji_px * 0.18
        else:
            if shadow:
                d.text((x + 1, y + 2), part, font=font, fill=INK_SHADOW)
            d.text((x, y), part, font=font, fill=fill)
            x += d.textlength(part, font=font)
    return x


def _mixed_width(text: str, font, emoji_px: int) -> float:
    """混排文本总宽度（供居中/右对齐计算）"""
    w = 0.0
    for part, is_emoji in _split_emoji(text):
        if is_emoji:
            w += emoji_px * 1.18
        else:
            w += _font_textlen(font, part)
    return w


def _wrap_mixed(text: str, font, max_width: float, emoji_px: int = 30) -> List[str]:
    """按像素宽度折行（中文逐字、emoji 整体、保留显式换行），用于长列表页"""
    text = str(text)
    if not text:
        return [""]
    if max_width <= 0:
        return [text]
    lines: List[str] = []
    for paragraph in text.split("\n"):
        if paragraph == "":
            lines.append("")
            continue
        cur = ""
        for part, is_emoji in _split_emoji(paragraph):
            if is_emoji:
                token_w = emoji_px * 1.18
                if cur and _mixed_width(cur, font, emoji_px) + token_w > max_width:
                    lines.append(cur)
                    cur = ""
                cur += part
                continue
            for ch in part:
                if cur and _mixed_width(cur + ch, font, emoji_px) > max_width:
                    lines.append(cur)
                    cur = ch
                else:
                    cur += ch
        lines.append(cur)
    return lines or [""]


def render_card(
    bg_name: str,
    title: str,
    subtitle: str = "",
    sections: Optional[List[Dict]] = None,
    footer: str = "",
    width: int = 1000,
) -> Optional[str]:
    """渲染国风绢帛·墨金·多字体层次信息卡片，返回图片路径（失败返回 None）"""
    if not HAS_PIL:
        return None

    bg_path = BG_DIR / f"{bg_name}.jpg"
    if not bg_path.exists():
        bg_path = BG_DIR / "default.jpg"
    if not bg_path.exists():
        return None

    sections = sections or []

    try:
        f_title = _load_font(56, "heavy")     # 大标题：最重
        f_section = _load_font(34, "bold")    # 区块名：碑刻
        f_value = _load_font(31, "semi")      # 数值：醒目
        f_label = _load_font(27, "text")      # 字段名：清峻（比数值小一号，拉开层次）
        f_foot = None                          # 脚注：行楷（按内容动态选择）
        f_xing_sub = None                      # 副标题：行楷（按内容动态选择）

        pad, inner = 54, 40
        row_h = 56
        section_gap = 32

        probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))

        # ===== 预排版：智能列宽（名称永不省略，超长自动换行）=====
        LABEL_RATIO_MIN = 0.30   # 字段列最少 30%
        VALUE_RATIO_MIN = 0.22   # 数值列最少 22%
        content_w = width - pad * 2 - inner * 2 - 40
        line_w = width - pad * 2 - inner * 2 - 10
        max_label_w = 0.0
        for sec in sections:
            for label, value in sec.get("rows", []):
                max_label_w = max(max_label_w, _font_textlen(f_label, str(label)))
        # 字段列按最长名称扩展；数值列至少保留 22%。
        # 两者下限冲突时优先保证名称完整，数值列改为折行显示。
        label_col = int(max(max_label_w + 6, content_w * LABEL_RATIO_MIN))
        label_col = min(label_col, int(content_w * (1 - VALUE_RATIO_MIN)))
        value_col = max(1, content_w - label_col)

        body_height = 0
        for sec in sections:
            body_height += row_h + 14
            for label, value in sec.get("rows", []):
                label_lines = _wrap_mixed(str(label), f_label, label_col)
                value_lines = _wrap_mixed(str(value), f_value, value_col)
                body_height += row_h * max(len(label_lines), len(value_lines))
            for line in sec.get("lines", []):
                body_height += row_h * max(1, len(_wrap_mixed(str(line), f_value, line_w, 30)))
            body_height += section_gap

        header_h = 176 if subtitle else 138
        footer_h = 70 if footer else 30
        height = max(640, header_h + inner + body_height + inner + footer_h + pad)

        # ===== 背景 =====
        bg = Image.open(bg_path).convert("RGB")
        canvas = _cover_resize(bg, width, height)
        canvas = ImageEnhance.Brightness(canvas).enhance(0.45)
        canvas = ImageEnhance.Color(canvas).enhance(0.85).convert("RGBA")
        grad = Image.new("L", (1, height))
        for y in range(height):
            grad.putpixel((0, y), int(200 - 60 * y / height))
        dark = Image.new("RGBA", (width, height), INK_BG_TOP + (0,))
        dark.putalpha(grad.resize((width, height)))
        canvas = Image.alpha_composite(canvas, dark)

        # ===== 绢帛面板 + 金边 =====
        px0, py0, px1, py1 = pad, header_h - 26, width - pad, height - footer_h
        overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        ImageDraw.Draw(overlay).rounded_rectangle([px0 - 3, py0 - 3, px1 + 3, py1 + 3], radius=30, fill=INK_ACCENT + (46,))
        canvas = Image.alpha_composite(canvas, overlay.filter(ImageFilter.GaussianBlur(8)))
        panel = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        ImageDraw.Draw(panel).rounded_rectangle([px0, py0, px1, py1], radius=26, fill=INK_PANEL)
        canvas = Image.alpha_composite(canvas, panel)

        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle([px0, py0, px1, py1], radius=26, outline=INK_PANEL_EDGE, width=3)
        draw.rounded_rectangle([px0 + 7, py0 + 7, px1 - 7, py1 - 7], radius=20, outline=INK_ACCENT + (120,), width=1)
        _draw_corner_clouds(draw, width, height)

        # ===== 鎏金大标题 =====
        title_s = _trunc(draw, title, f_title, width - pad * 2 - 60)
        # 混排量宽（emoji 镀金位图 + 中文）
        emoji_px_title = 46
        tw = _mixed_width(title_s, f_title, emoji_px_title)
        _draw_mixed(canvas, ((width - tw) / 2, 38), title_s, f_title, emoji_px_title, None)
        draw = ImageDraw.Draw(canvas)
        # 两侧卷轴金线
        line_y = 40 + 40
        gap = tw / 2 + 26
        draw.line([(width / 2 - gap - 150, line_y), (width / 2 - gap, line_y)], fill=INK_ACCENT, width=2)
        draw.line([(width / 2 + gap, line_y), (width / 2 + gap + 150, line_y)], fill=INK_ACCENT, width=2)
        draw.ellipse([width / 2 - gap - 5, line_y - 4, width / 2 - gap + 3, line_y + 4], outline=INK_ACCENT, width=2)
        draw.ellipse([width / 2 + gap - 3, line_y - 4, width / 2 + gap + 5, line_y + 4], outline=INK_ACCENT, width=2)

        # ===== 行楷副标题（emoji 镀金点缀）=====
        y_cursor = py0 + inner
        if subtitle:
            f_xing_sub = _xing_font_for(subtitle, 30)
            sub_s = _trunc(draw, subtitle, f_xing_sub, width - pad * 2 - inner * 2 - 60)
            emoji_px_sub = 30
            sw = _mixed_width(sub_s, f_xing_sub, emoji_px_sub)
            _draw_mixed(canvas, ((width - sw) / 2, 112), sub_s, f_xing_sub, emoji_px_sub, INK_GOLD_DIM)
            draw = ImageDraw.Draw(canvas)

        # ===== 区块与行（智能列宽版 + emoji 混排）=====
        label_x = pad + inner + 16
        value_right = width - pad - inner - 10
        emoji_px_val = 30

        for sec in sections:
            name = _trunc(draw, sec.get("name", ""), f_section, width - pad * 2 - inner * 2 - 40)
            # 区块名金箔底衬
            nw = draw.textlength(name, font=f_section)
            bx0, by0 = label_x + 14, y_cursor - 2
            draw.rounded_rectangle([bx0, by0, bx0 + nw + 18, by0 + 42], radius=8,
                                   fill=INK_ACCENT + (52,), outline=INK_ACCENT + (150,), width=1)
            cx = label_x + 4
            draw.polygon([(cx - 9, y_cursor + 19), (cx, y_cursor + 10), (cx + 9, y_cursor + 19), (cx, y_cursor + 28)],
                         outline=INK_SECTION, width=2)
            draw.text((label_x + 22, y_cursor + 1), name, font=f_section, fill=INK_SECTION)
            draw.line([(label_x + 22 + nw + 26, y_cursor + 19), (width - pad - inner, y_cursor + 19)],
                      fill=INK_DIVIDER, width=1)
            y_cursor += row_h + 8

            for label, value in sec.get("rows", []):
                label_lines = _wrap_mixed(str(label), f_label, label_col) or [""]
                value_lines = _wrap_mixed(str(value), f_value, value_col) or [""]
                line_count = max(len(label_lines), len(value_lines))
                for idx in range(line_count):
                    if idx < len(label_lines):
                        draw.text((label_x + 6, y_cursor + 3), label_lines[idx],
                                  font=f_label, fill=INK_LABEL)
                    if idx < len(value_lines):
                        vw = _mixed_width(value_lines[idx], f_value, emoji_px_val)
                        _draw_mixed(canvas, (value_right - vw, y_cursor), value_lines[idx],
                                    f_value, emoji_px_val, INK_TEXT)
                        draw = ImageDraw.Draw(canvas)
                    # 行间点线只用于单行条目，避免多行名称时穿插杂乱
                    if idx == 0 and line_count == 1:
                        lx_end = label_x + 6 + draw.textlength(label_lines[0], font=f_label)
                        vw = _mixed_width(value_lines[0], f_value, emoji_px_val)
                        for dx in range(int(lx_end + 12), int(value_right - vw - 14), 26):
                            draw.ellipse([dx, y_cursor + 27, dx + 3, y_cursor + 29],
                                         fill=INK_DIVIDER + (110,))
                y_cursor += row_h * line_count

            for line in sec.get("lines", []):
                for seg in _wrap_mixed(str(line), f_value, line_w, emoji_px_val):
                    _draw_mixed(canvas, (label_x + 6, y_cursor), seg, f_value, emoji_px_val, INK_TEXT)
                    draw = ImageDraw.Draw(canvas)
                    y_cursor += row_h

            y_cursor += section_gap

        # ===== 行楷脚注（emoji 镀金点缀）=====
        if footer:
            f_foot = _xing_font_for(footer, 26)
            foot_s = _trunc(draw, footer, f_foot, width - pad * 2)
            fw = _mixed_width(foot_s, f_foot, 26)
            draw.line([(pad + 60, height - footer_h + 8), (width - pad - 60, height - footer_h + 8)],
                      fill=INK_DIVIDER + (140,), width=1)
            _draw_mixed(canvas, ((width - fw) / 2, height - footer_h + 14), foot_s, f_foot, 26, INK_FOOTER)

        # ===== 落盘 =====
        CARD_TEMP_DIR.mkdir(parents=True, exist_ok=True)
        out_path = CARD_TEMP_DIR / f"card_{uuid.uuid4().hex[:12]}.jpg"
        canvas.convert("RGB").save(str(out_path), format="JPEG", quality=92)
        try:
            cards = sorted(CARD_TEMP_DIR.glob("card_*.jpg"), key=os.path.getmtime)
            for old in cards[:-40]:
                old.unlink(missing_ok=True)
        except Exception:
            pass
        return str(out_path)
    except Exception as e:
        logger.error(f"渲染卡片失败: {e}")
        return None


async def render_card_async(bg_name: str, title: str, subtitle: str = "",
                            sections: Optional[List[Dict]] = None, footer: str = "",
                            width: int = 1000) -> Optional[str]:
    """异步版本；失败/关闭返回 None"""
    import asyncio
    return await asyncio.to_thread(render_card, bg_name, title, subtitle, sections, footer, width)


# ===== 兼容旧接口 =====
if HAS_PIL:
    class ImageGenerator:
        def __init__(self):
            self.has_pil = HAS_PIL

        async def generate_user_info_card(self, user_id: str, detail_map: Dict) -> Optional[BytesIO]:
            path = render_card("info", detail_map.get("道号", f"道友{user_id}"), f"ID: {user_id}",
                               sections=[{"name": "基本信息", "rows": [(k, str(v)) for k, v in detail_map.items() if k != "道号"]}],
                               )
            if path:
                with open(path, "rb") as f:
                    return BytesIO(f.read())
            return None
else:
    class ImageGenerator:
        def __init__(self):
            self.has_pil = False

        async def generate_user_info_card(self, user_id: str, detail_map: Dict):
            return None
