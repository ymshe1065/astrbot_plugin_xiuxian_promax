# -*- coding: utf-8 -*-
"""修仙小人动作集生成器：身体形变 + 道具 + 特效。

画布 48×48，角色中心 x=24，脚底对齐 y=44。
动作共 7 套：idle / walk / float / alchemy / slash / look / cast。
帧数参照 LPC（Liberated Pixel Cup，像素角色动画事实标准）：
idle 2、walk 8、slash 6、cast 7、sit 3。

输入素材在 assets/，是 PixelLab 生成角色的 side（east）方向图：
    east_stand_32x48.png  站立，角色实际占 y=8~44
    east_sit_32x32.png    跪坐，贴到 y=13 起，用于闭关与炼丹

输出 gen/_acts.js，内容是一行 ``const SPR_B64 = {...}``，
供 build_demo.py 生成演示页，也供 sync_preview.py 注入 preview.html。

用法::

    python scripts/anim/acts.py           # 生成 gen/_acts.js
    python scripts/anim/build_demo.py     # 生成 static/anim/acts.html
    python scripts/anim/sync_preview.py   # 把动作数据注入 preview.html

设计取舍（踩过的坑，改之前先看）：
1. 不做手臂。32px 尺度上手臂必然是个方块，且与躯干衔接断裂；
   动作靠身体形变 + 道具（丹炉/悬浮剑）+ 特效（剑气弧/法阵/云气）表达。
2. 身体不做重绘，只对原图按行重采样（rig.warp），保住 PixelLab 画的
   褶皱和 1px 描边；重绘过的版本会变成没有腰的钟形。
3. 画布从 32 加宽到 48，否则 14px 长的剑会被裁掉半截。
"""
import sys, os, math, json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rig import *                      # noqa: F401,F403  (blank/px/warp/lean_fn/调色板)
from pose import line, bob, hem_swing  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, 'assets')
GEN = os.path.join(HERE, 'gen')

OX = 8
_SRC32 = Image.open(os.path.join(ASSETS, 'east_stand_32x48.png')).convert('RGBA')
_SIT32 = Image.open(os.path.join(ASSETS, 'east_sit_32x32.png')).convert('RGBA')

SRC = blank(); SRC.alpha_composite(_SRC32, (OX, 0))
HEAD = blank(); HEAD.alpha_composite(_SRC32.crop((0, 8, 32, 22)), (OX, 8))
HEAD_L = blank(); HEAD_L.alpha_composite(_SRC32.crop((0, 8, 32, 22)).transpose(Image.FLIP_LEFT_RIGHT), (OX + 1, 8))
SIT = blank(); SIT.alpha_composite(_SIT32, (OX, 13))

GOLD   = (0xFF, 0xD9, 0x7A, 255)
GOLD2  = (0xFF, 0xB4, 0x4D, 255)
HOT    = (0xFF, 0xF0, 0xC0, 255)
JADE2  = (0xBF, 0xF0, 0xE0, 255)
STEEL  = (0xE8, 0xF4, 0xFA, 255)
STEEL_D= (0x9A, 0xB8, 0xCC, 255)
CLOUD_W= (0xF4, 0xF9, 0xFD, 255)
CLOUD_B= (0xC6, 0xDA, 0xEC, 255)
CLOUD_C= (0x8A, 0xA4, 0xBC, 255)
CLOUD_E= (0x66, 0x80, 0x9C, 255)
BRONZE = (0x6E, 0x5C, 0x44, 255)
BRONZE_L=(0xA8, 0x92, 0x66, 255)
BRONZE_D=(0x3C, 0x31, 0x25, 255)
RUNE   = (0x8F, 0xF0, 0xD8, 255)


def base(lean=0, bend=0, scale=1.0, dx=0, dy=0, head_dx=0):
    im = blank()
    f = lean_fn(lean, bend)
    if scale != 1.0:
        g = f
        f = lambda y: (g(y)[0], g(y)[1] * (scale if 26 <= y <= 40 else 1.0))
    im.alpha_composite(warp(SRC, f), (dx, dy))
    im.alpha_composite(HEAD, (int(round(lean)) + dx + head_dx, dy))
    return im


def arc(img, cx, cy, r, a0, a1, col, thick=0):
    n = max(2, int(abs(a1 - a0) * 2.4))
    for i in range(n + 1):
        a = math.radians(a0 + (a1 - a0) * i / n)
        x, y = cx + math.cos(a) * r, cy + math.sin(a) * r
        px(img, x, y, col)
        for t in range(1, thick + 1):
            px(img, x, y + t, (col[0], col[1], col[2], max(0, col[3] - 120 * t)))


def puff(img, cx, cy, w, c=CLOUD_W, s=CLOUD_B):
    """蓬松云团：中间隆起、两端收低的拱形"""
    half = w // 2
    for x in range(cx - half, cx + half + 1):
        d = abs(x - cx) / float(max(1, half))
        h = 3 if d < 0.34 else (2 if d < 0.72 else 1)
        for k in range(h):
            px(img, x, cy - k, c if k < h - 1 else s)
    # 底部云影 + 深色轮廓（保证在白色云海上也能看见）
    for x in range(cx - half + 1, cx + half):
        px(img, x, cy + 1, s)
    for x in range(cx - half, cx + half + 1):
        px(img, x, cy + 2, CLOUD_E)


# ---------------------------------------------------------------- 站立待机
def a_idle(n=4):
    out = []
    for i in range(n):
        ph = math.sin(i / n * 2 * math.pi)
        out.append(bob(SRC, 0 if ph > 0 else -1))
    return out


# ---------------------------------------------------------------- 漂浮打坐
def a_float(n=6):
    out = []
    for i in range(n):
        ph = math.sin(i / n * 2 * math.pi)
        lift = -12 + int(round(ph))
        im = blank()
        foot = 44 + lift
        puff(im, 15, foot - 2, 10)
        puff(im, 33, foot - 1, 10)
        puff(im, 24, foot, 15, c=CLOUD_W, s=CLOUD_C)
        im.alpha_composite(SIT, (0, lift))
        for j in range(7):
            a = i / n * 2 * math.pi + j * math.pi * 2 / 7
            cx = 24 + math.cos(a) * 11.5
            cy = 25 + lift + math.sin(a) * 5
            px(im, cx, cy, GOLD if j % 2 else JADE2)
        out.append(im)
    return out


# ---------------------------------------------------------------- 炼药
def a_alchemy(n=6):
    out = []
    for i in range(n):
        im = base(lean=4, bend=6, scale=0.99)
        CXF = 39
        # 炉腹：上下窄、中间鼓
        for y in range(35, 43):
            t = (y - 35) / 7.0
            w = 4 + math.sin(t * math.pi) * 6
            x0 = int(round(CXF - w / 2)); x1 = int(round(CXF + w / 2))
            for x in range(x0, x1 + 1):
                d = x - x0
                c = BRONZE_L if d <= 1 else (BRONZE if d < (x1 - x0) - 1 else BRONZE_D)
                px(im, x, y, c)
            px(im, x0 - 1, y, BLACK); px(im, x1 + 1, y, BLACK)
        # 鼎耳
        px(im, CXF - 4, 35, BRONZE_L); px(im, CXF - 5, 34, BRONZE_L)
        px(im, CXF + 4, 35, BRONZE_L); px(im, CXF + 5, 34, BRONZE_L)
        px(im, CXF - 5, 33, BLACK); px(im, CXF + 5, 33, BLACK)
        # 炉口
        for x in range(CXF - 3, CXF + 4):
            px(im, x, 34, BRONZE_D)
            px(im, x, 33, BLACK)
        # 三足
        for fx in (CXF - 3, CXF, CXF + 3):
            px(im, fx, 43, BRONZE_D); px(im, fx, 44, BLACK)
        # 火焰：3 层，帧间跳动
        fh = [5, 6, 4, 6, 5, 7][i % 6]
        for k in range(fh):
            yy = 32 - k
            wdt = max(1, int(3.2 - k * 0.42))
            for x in range(CXF - wdt, CXF + wdt + 1):
                d = abs(x - CXF) / float(max(1, wdt))
                col = HOT if d < 0.4 and k < fh - 2 else (GOLD if d < 0.75 else GOLD2)
                px(im, x, yy, col)
        # 火焰外圈光晕
        for k in range(3):
            px(im, CXF - 4 + (k % 2), 31 - k, (0xFF, 0xC8, 0x70, 90))
            px(im, CXF + 4 - (k % 2), 31 - k, (0xFF, 0xC8, 0x70, 90))
        # 药气
        for j in range(3):
            t = (i / float(n) + j / 3.0) % 1.0
            cx = CXF + math.sin(t * 6.28 + j * 2) * 2.6
            cy = 26 - t * 15
            px(im, cx, cy, (0xBF, 0xF0, 0xE0, int(190 * (1 - t))))
        out.append(im)
    return out


# ---------------------------------------------------------------- 御剑挥砍
def a_slash(n=6):
    # (前冲dx, 倾斜, 剑角, 弧起, 弧止)
    F = [(0, -3, -44, 0, 0),
         (-2, -6, -70, -118, -70),
         (2, 1, -12, -112, -40),
         (4, 7, 16, -86, 24),
         (3, 5, 44, -32, 80),
         (1, 2, 30, 40, 92)]
    out = []
    for i, (dx, ln, ang, a0, a1) in enumerate(F):
        im = base(lean=ln, bend=ln * 0.5, dx=dx, dy=-1 if i in (2, 3) else 0)
        cx, cy = 37 + dx, 27
        if a0:
            arc(im, cx, cy, 11, a0, a1, (0xFF, 0xFF, 0xFF, 235), thick=2)
            arc(im, cx, cy, 13, a0 + 7, a1 - 7, (0x9A, 0xE8, 0xD8, 110))
        rad = math.radians(ang)
        bx, by = cx - math.cos(rad) * 5, cy - math.sin(rad) * 5
        tx, ty = cx + math.cos(rad) * 11, cy + math.sin(rad) * 11
        line(im, bx, by, tx, ty, STEEL)
        line(im, bx, by + 1, tx, ty + 1, STEEL_D)
        nx_, ny_ = -math.sin(rad), math.cos(rad)
        for m in (-1, 0, 1):
            px(im, bx + nx_ * m, by + ny_ * m, GOLD)
        out.append(im)
    return out


# ---------------------------------------------------------------- 走路
# 脚单独提出来，才能在走路时左右平移；袍摆单独摆动
FEET = blank(); FEET.alpha_composite(SRC.crop((0, 42, W, 45)), (0, 42))
NOFEET = blank()
for _y in range(42):
    for _x in range(W):
        NOFEET.putpixel((_x, _y), SRC.getpixel((_x, _y)))


def a_walk(n=8):
    out = []
    for i in range(n):
        ph = math.sin(i / n * 2 * math.pi)
        im = hem_swing(NOFEET, 6.0, ph)
        up = -1 if abs(ph) > 0.55 else 0
        im = bob(im, up)
        im.alpha_composite(FEET, (int(round(ph * 3.2)), up))
        out.append(im)
    return out


# ---------------------------------------------------------------- 张望看货
def a_look(n=6):
    """左右张望：头部镜像转向，身体只做轻微侧移。

    平移 1px 在 32px 尺度上完全看不出来，镜像头才有「转头」的读感。
    """
    seq = [(0, 0, 0), (0, 0, 1), (1, 1, 1), (0, 0, 0), (-1, -1, -1), (-1, -1, -1)]
    out = []
    for i in range(n):
        sx, _, side = seq[i % n]
        im = base(lean=sx * 0.5, bend=sx * 0.3, dx=sx)
        if side == -1:
            # 用镜像头覆盖
            for y in range(8, 22):
                for x in range(W):
                    v = HEAD_L.getpixel((x, y))
                    if v[3] > 0:
                        px(im, x, y, v)
                    elif HEAD.getpixel((x, y))[3] > 0:
                        im.putpixel((x, y), (0, 0, 0, 0))
        out.append(im)
    return out


# ---------------------------------------------------------------- 运功施法
def a_cast(n=7):
    out = []
    for i in range(n):
        t = i / float(n - 1)
        im = base(lean=-1 - t * 2, bend=0, scale=1.0, dy=int(-t * 1.5))
        # 脚下法阵（浅色地板上必须带深色底，否则看不见）
        r = 7 + t * 2
        for a in range(0, 360, 9):
            ar = math.radians(a)
            xx, yy = 24 + math.cos(ar) * r, 44 + math.sin(ar) * r * 0.38
            px(im, xx, yy + 1, (0x1E, 0x5A, 0x50, 150))
            px(im, xx, yy, (0x5F, 0xE0, 0xBE, 130 + int(t * 90)))
        for a in range(0, 360, 45):
            ar = math.radians(a)
            px(im, 24 + math.cos(ar) * r, 44 + math.sin(ar) * r * 0.38, RUNE)
        # 掌前光球上升
        by = 30 - t * 13
        for dy_, w in ((0, 1), (1, 3), (2, 4), (3, 3), (4, 1)):
            for x in range(-w // 2, w // 2 + 1):
                a = 210 - dy_ * 30
                px(im, 33 + x, by + dy_, (0xDF, 0xFF, 0xF4, max(30, a)))
        px(im, 33, int(by) + 2, (255, 255, 255, 250))
        out.append(im)
    return out


ACTS = {
    'idle':    a_idle(),
    'walk':    a_walk(),
    'float':   a_float(),
    'alchemy': a_alchemy(),
    'slash':   a_slash(),
    'look':    a_look(),
    'cast':    a_cast(),
}


def make_js():
    """把所有帧编码成 base64，返回一行 JS 常量声明"""
    data = {}
    for k, fr in ACTS.items():
        data[k] = [b64(f) for f in fr]
    return "const SPR_B64 = " + json.dumps(data, ensure_ascii=False) + ";"


if __name__ == '__main__':
    js = make_js()
    os.makedirs(GEN, exist_ok=True)
    path = os.path.join(GEN, '_acts.js')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(js)
    for k, fr in ACTS.items():
        print('%-8s %2d 帧' % (k, len(fr)))
    print('尺寸 %d×%d，输出 %s（%.1f KB）' % (W, H, path, len(js) / 1024.0))
