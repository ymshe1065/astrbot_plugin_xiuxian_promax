# -*- coding: utf-8 -*-
"""更新 Android 各密度启动屏为带头像的版本"""
from PIL import Image
from pathlib import Path

LOGO = Path(r"C:/use/code/project/xiuxianbot/astrbot_plugin_xiuxian_promax/webchat_room/static/img/whale_logo.png")
RES = Path(r"C:/use/code/project/xiuxianbot/app/android/app/src/main/res")
BG = (238, 244, 255, 255)

def make(w, h):
    img = Image.new("RGBA", (w, h), BG)
    side = int(min(w, h) * 0.32)
    logo = Image.open(LOGO).convert("RGBA").resize((side, side), Image.LANCZOS)
    img.paste(logo, ((w - side) // 2, (h - side) // 2 - int(h * 0.04)), logo)
    return img

# 竖屏
for dpi, w in {"mdpi": 480, "hdpi": 720, "xhdpi": 960, "xxhdpi": 1440, "xxxhdpi": 1920}.items():
    d = RES / f"drawable-port-{dpi}"
    d.mkdir(parents=True, exist_ok=True)
    h = int(w * 1280 / 720)
    make(w, h).save(d / "splash.png")

# 横屏
for dpi, h in {"mdpi": 480, "hdpi": 720, "xhdpi": 960, "xxhdpi": 1440, "xxxhdpi": 1920}.items():
    d = RES / f"drawable-land-{dpi}"
    d.mkdir(parents=True, exist_ok=True)
    w = int(h * 1280 / 720)
    make(w, h).save(d / "splash.png")

# 默认 drawable/splash.png 也用带头像版
make(720, 1280).save(RES / "drawable" / "splash.png")

print("启动屏已全部更新")
