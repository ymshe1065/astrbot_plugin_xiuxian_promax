# -*- coding: utf-8 -*-
"""生成「肥鱼修仙」App 图标（复用现有二次元少女头像 whale_logo.png）

- Android: 生成 mipmap-* 各密度的 ic_launcher / ic_launcher_round / ic_launcher_foreground
- iOS: 生成 AppIcon.appiconset 需要的各尺寸（用圆形裁切 + 圆角方底）
"""
from PIL import Image, ImageDraw, ImageOps
from pathlib import Path
import shutil

SRC = Path(r"C:/use/code/project/xiuxianbot/astrbot_plugin_xiuxian_promax/webchat_room/static/img/whale_logo.png")
APP = Path(r"C:/use/code/project/xiuxianbot/app")

BG = (238, 244, 255, 255)  # 浅蓝底，和网页主题一致 #eef4ff


def make_round_icon(size: int, circular: bool) -> Image.Image:
    """把源头像缩放铺满，生成圆形或圆角方形图标（带浅蓝底）。"""
    base = Image.new("RGBA", (size, size), BG)
    im = Image.open(SRC).convert("RGBA")
    # 缩放铺满
    im = im.resize((size, size), Image.LANCZOS)
    if circular:
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    else:
        mask = Image.new("L", (size, size), 0)
        r = int(size * 0.18)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, size, size), radius=r, fill=255)
    base.paste(im, (0, 0), mask)
    return base


# ---- Android mipmap ----
densities = {
    "mdpi": 48, "hdpi": 72, "xhdpi": 96,
    "xxhdpi": 144, "xxxhdpi": 192,
}
android_res = APP / "android" / "app" / "src" / "main" / "res"
for name in ["ic_launcher", "ic_launcher_round", "ic_launcher_foreground"]:
    for dpi, px in densities.items():
        d = android_res / f"mipmap-{dpi}"
        d.mkdir(parents=True, exist_ok=True)
        circular = (name == "ic_launcher_round")
        # foreground 用透明背景的圆角图形
        if name == "ic_launcher_foreground":
            img = make_round_icon(px, circular=False)
            # foreground 建议带安全边距：缩放 66% 居中
            inner = int(px * 0.66)
            fg = make_round_icon(inner, circular=False)
            img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
            img.paste(fg, ((px - inner) // 2, (px - inner) // 2))
        else:
            img = make_round_icon(px, circular=circular)
        img.save(d / f"{name}.png")

print("Android 图标已生成")

# ---- iOS AppIcon.appiconset ----
ios_set = APP / "ios" / "App" / "App" / "Assets.xcassets" / "AppIcon.appiconset"
ios_set.mkdir(parents=True, exist_ok=True)
ios_sizes = {
    "icon-20.png": 20, "icon-20@2x.png": 40, "icon-20@3x.png": 60,
    "icon-29.png": 29, "icon-29@2x.png": 58, "icon-29@3x.png": 87,
    "icon-40.png": 40, "icon-40@2x.png": 80, "icon-40@3x.png": 120,
    "icon-60@2x.png": 120, "icon-60@3x.png": 180,
    "icon-76.png": 76, "icon-76@2x.png": 152,
    "icon-83.5@2x.png": 167,
    "icon-1024.png": 1024,
}
for fname, px in ios_sizes.items():
    img = make_round_icon(px, circular=False)
    img.save(ios_set / fname)

print("iOS 图标已生成")

# ---- 启动屏图片 ----
sp_dir = APP / "android" / "app" / "src" / "main" / "res" / "drawable"
sp_dir.mkdir(parents=True, exist_ok=True)
sp = Image.new("RGBA", (720, 1280), BG)
logo = make_round_icon(360, circular=True)
sp.paste(logo, ((720 - 360) // 2, (1280 - 360) // 2 - 80), logo)
sp.save(sp_dir / "splash.png")
print("Android 启动屏已生成")
