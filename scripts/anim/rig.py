# -*- coding: utf-8 -*-
"""分层角色 rig：库存头（PixelLab）+ 程序化躯干/袖/摆"""
from PIL import Image
import math, io, base64

W, H = 48, 48
PX = 8          # 32宽原图居中到48画布的左偏移
GY = 44                      # 脚底基准行
CX = 24                      # 角色中心 x
NECK = 22                    # 颈（头底下一行）
WAIST = 33

BLACK = (0, 0, 0, 255)
T   = (0x59, 0xB9, 0xA9, 255)   # 主
TL  = (0x7F, 0xCE, 0xBF, 255)   # 亮
TD  = (0x40, 0x89, 0x89, 255)   # 暗
TP  = (0x35, 0x66, 0x7C, 255)   # 最深
SKIN= (0xFA, 0xC9, 0x9F, 255)
HAIR= (0x2A, 0x1D, 0x29, 255)

def blank():
    return Image.new('RGBA', (W, H), (0, 0, 0, 0))

def px(img, x, y, c):
    if 0 <= int(x) < W and 0 <= int(y) < H:
        img.load()[int(x), int(y)] = c

def bar(img, x0, x1, y, c, edge=None):
    """画一行实心条，左右各加 1px 描边"""
    x0, x1 = int(round(x0)), int(round(x1))
    for x in range(x0, x1 + 1):
        px(img, x, y, c)
    if edge is not False:
        px(img, x0 - 1, y, BLACK)
        px(img, x1 + 1, y, BLACK)

def body(img, lean=0.0, bend=0.0, hem=0.0, w_top=6, w_waist=9, w_hem=13,
         y0=NECK, y1=GY):
    """躯干+袍。lean: 上身水平偏移；bend: 腰部额外偏移；hem: 摆底偏移"""
    for y in range(y0, y1 + 1):
        if y <= WAIST:
            t = (y - y0) / max(1, (WAIST - y0))
            cx = 16 + lean * (1 - t) + bend * t
            w = w_top + (w_waist - w_top) * t
        else:
            t = (y - WAIST) / max(1, (y1 - WAIST))
            cx = 16 + bend + hem * (t ** 1.5)
            w = w_waist + (w_hem - w_waist) * (t ** 0.7)
        x0 = cx - w / 2.0
        x1 = cx + w / 2.0
        # 横向明暗：左上受光，右下压暗
        bar(img, x0 + 1, x1 - 1, y, T)
        px(img, x0, y, BLACK); px(img, x1, y, BLACK)
        # 受光边（左）
        if y > y0 + 1:
            px(img, x0 + 1, y, TL)
        # 暗边（右）
        px(img, x1 - 1, y, TD)
        px(img, x1 - 2, y, TD)
    # 摆底一行压最深
    for x in range(0, W):
        c = img.load()[x, y1]
        if c[3] > 0 and c[:3] not in (BLACK[:3],):
            px(img, x, y1, TP)

def sleeve(img, sx, sy, ang, upper=4, fore=5, w0=3, w1=4):
    """袖子：肩出发沿角度伸出手臂，末端加宽做袖口"""
    a = math.radians(ang)
    ex = sx + math.cos(a) * upper
    ey = sy + math.sin(a) * upper
    hx = ex + math.cos(a) * fore
    hy = ey + math.sin(a) * fore
    # 上臂
    steps = int(max(abs(hx - sx), abs(hy - sy))) + 1
    for i in range(steps + 1):
        t = i / max(1, steps)
        cx = sx + (ex - sx) * t
        cy = sy + (ey - sy) * t
        half = w0 / 2.0
        for d in range(-2, 3):
            yy = int(round(cy + d))
            if abs(d) <= half:
                px(img, cx, yy, T if d < 0 else TD)
        px(img, cx - half - 1, cy, BLACK)
        px(img, cx + half + 1, cy, BLACK)
    # 袖口（梯形）
    steps = int(max(abs(hx - ex), abs(hy - ey))) + 1
    for i in range(steps + 1):
        t = i / max(1, steps)
        cx = ex + (hx - ex) * t
        cy = ey + (hy - ey) * t
        half = (w0 + (w1 - w0) * t) / 2.0
        for d in range(-3, 4):
            if abs(d) <= half:
                px(img, cx, cy + d, TL if d < 0 else TD)
            elif abs(d) <= half + 1:
                px(img, cx, cy + d, BLACK)
    # 手
    px(img, hx, hy, SKIN)
    px(img, hx, hy + 1, SKIN)
    return hx, hy

def feet(img, spread=1, boots=(0,)):
    for b in boots:
        bx = 16 + int(spread * (1 if b == 1 else -1))
        for x in range(bx - 1, bx + 2):
            px(img, x, GY, BLACK)
        for x in range(bx, bx + 2):
            px(img, x, GY - 1, HAIR)
        px(img, bx + 2, GY, BLACK)

def b64(img):
    buf = io.BytesIO()
    img.save(buf, 'PNG', optimize=True)
    return base64.b64encode(buf.getvalue()).decode()

def head_from(src, top=8, bot=21):
    return src.crop((0, top, W, bot + 1)), top


# ============ 轮廓变形（保留原始内部细节） ============
def warp(img, f):
    """f(y) -> (dx, scale)。按行重采样，保留原图明暗与褶皱"""
    src = img.load()
    out = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    o = out.load()
    for y in range(H):
        xs = [x for x in range(W) if src[x, y][3] > 0]
        if not xs:
            continue
        x0, x1 = min(xs), max(xs)
        dx, sc = f(y)
        w = x1 - x0 + 1
        nw = max(1, int(round(w * sc)))
        cx = (x0 + x1) / 2.0 + dx
        nx0 = int(round(cx - nw / 2.0))
        for k in range(nw):
            s = min(x1, x0 + int(k * w / nw))
            nx = nx0 + k
            if 0 <= nx < W:
                o[nx, y] = src[s, y]
    return out


def lean_fn(lean=0.0, bend=0.0, waist=33, top=22):
    """上身前倾：从头顶到腰线性偏移，腰以下几乎不动"""
    def f(y):
        if y <= top:
            return (lean, 1.0)
        if y <= waist:
            t = (y - top) / float(waist - top)
            return (lean * (1 - t) + bend * t, 1.0)
        t = min(1.0, (y - waist) / 11.0)
        return (bend * (1 - t * 0.35), 1.0 + 0.02 * t)
    return f


def arm_under(img, sx, sy, ang, upper=5, fore=6, w0=3, w1=5):
    """在身体之前调用：手臂根部会被躯干盖住，形成自然衔接"""
    return sleeve(img, sx, sy, ang, upper, fore, w0, w1)
