# -*- coding: utf-8 -*-
"""修仙小人姿态引擎：以 PixelLab 角色为底，做分层变换 + 道具叠加"""
from PIL import Image
import base64, io, math

W, H = 32, 48
TOP, BOT = 8, 44          # 角色实际占位
HEAD_Y0, HEAD_Y1 = 8, 21  # 头
NECK = 22
WAIST = 34
FEET = 42

BLACK = (0, 0, 0, 255)
T = (0x59, 0xB9, 0xA9, 0xFF)   # 袍主色
TL = (0x7F, 0xCE, 0xBF, 0xFF)  # 受光
TD = (0x40, 0x89, 0x89, 0xFF)  # 暗
TP = (0x35, 0x66, 0x7C, 0xFF)  # 最深
SKIN = (0xFA, 0xC9, 0x9F, 0xFF)
HAIR = (0x2A, 0x1D, 0x29, 0xFF)
JADE = (0x59, 0xB9, 0xA9, 0xFF)

def load(path):
    return Image.open(path).convert('RGBA')

def shear(img, k, pivot=BOT, xmask=None):
    """全身连续斜切：dx = k*(pivot-y)。整图统一，保证不撕裂"""
    W_, H_ = img.size
    src = img.load()
    out = Image.new('RGBA', (W_, H_), (0, 0, 0, 0))
    o = out.load()
    for y in range(H_):
        dx = int(round(k * (pivot - y)))
        for x in range(W_):
            c = src[x, y]
            if c[3] == 0:
                continue
            nx = x + dx
            if 0 <= nx < W_:
                o[nx, y] = c
    return out

def bob(img, dy):
    W_, H_ = img.size
    out = Image.new('RGBA', (W_, H_), (0, 0, 0, 0))
    out.alpha_composite(img, (0, dy))
    return out

def hem_swing(img, amp, phase):
    """只让腰以下逐行水平摆动，上半身冻结"""
    W_, H_ = img.size
    src = img.load()
    out = Image.new('RGBA', (W_, H_), (0, 0, 0, 0))
    o = out.load()
    for y in range(H_):
        for x in range(W_):
            c = src[x, y]
            if c[3] == 0:
                continue
            if y >= WAIST:
                t = (y - WAIST) / max(1, (BOT - WAIST))
                dx = int(round((t ** 1.3) * amp * phase))
            else:
                dx = 0
            nx = x + dx
            if 0 <= nx < W_:
                o[nx, y] = c
    return out

def px(img, x, y, c):
    if 0 <= x < W and 0 <= y < H:
        img.load()[x, y] = c

def rect(img, x0, y0, w, h, c):
    for y in range(y0, y0 + h):
        for x in range(x0, x0 + w):
            px(img, x, y, c)

def line(img, x0, y0, x1, y1, c):
    dx = abs(x1 - x0); dy = abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx - dy
    n = 0
    lim = dx + dy + 2
    while n < lim:
        px(img, x0, y0, c)
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 > -dy:
            err -= dy; x0 += sx
        if e2 < dx:
            err += dx; y0 += sy
        n += 1

def b64(img):
    buf = io.BytesIO()
    img.save(buf, 'PNG', optimize=True)
    return base64.b64encode(buf.getvalue()).decode()
