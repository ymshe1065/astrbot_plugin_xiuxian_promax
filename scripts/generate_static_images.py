#!/usr/bin/env python3
# scripts/generate_static_images.py
"""生成固定接口列表页图片（如 /修仙帮助）。

用法:
    python scripts/generate_static_images.py --list
    python scripts/generate_static_images.py --page help
    python scripts/generate_static_images.py --all --force
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name


def _load(module_name: str):
    return importlib.import_module(f"{PKG}.utils.{module_name}")


async def _run(pages, force: bool, all_pages: bool):
    generator = _load("static_image_generator")
    page_specs = _load("static_pages").PAGES
    targets = list(page_specs) if all_pages else pages
    for page_key in targets:
        if page_key not in page_specs:
            print(f"[跳过] 未注册页面: {page_key}")
            continue
        result = await generator.generate_page(page_key, force=force)
        if result:
            path = Path(result)
            size = path.stat().st_size if path.exists() else 0
            print(f"[完成] {page_key}: {path} ({size} bytes)")
        else:
            print(f"[失败] {page_key}: 未生成图片（检查 Pillow/字体/背景资源）")
            return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="生成修仙插件固定接口列表页图片")
    parser.add_argument("--page", action="append", default=[], help="要生成的页面 key，可重复指定")
    parser.add_argument("--all", action="store_true", help="生成全部已注册页面")
    parser.add_argument("--force", action="store_true", help="忽略内容指纹，强制重绘")
    parser.add_argument("--list", action="store_true", help="列出已注册页面")
    args = parser.parse_args()

    page_specs = _load("static_pages").PAGES
    if args.list:
        for key, spec in page_specs.items():
            print(f"{key}\t{spec.get('title', '')}")
        return 0

    if not args.page and not args.all:
        args.all = True
    return asyncio.run(_run(args.page, args.force, args.all))


if __name__ == "__main__":
    raise SystemExit(main())
