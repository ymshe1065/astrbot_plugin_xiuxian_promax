# utils/static_image_generator.py
"""固定接口列表页图片生成与缓存（清单页一次生成，调用时直接发送）"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from astrbot.api import logger
except ImportError:  # 允许脱离 AstrBot 运行生成脚本/离线测试
    import logging
    logger = logging.getLogger("xiuxian.static_image_generator")

from .image_generator import HAS_PIL, render_card_async
from .static_pages import PAGES, page_fingerprint

RES_DIR = Path(__file__).resolve().parents[1] / "resources"
OUTPUT_DIR = RES_DIR / "generated"
MANIFEST_PATH = OUTPUT_DIR / "manifest.json"

_AUTO_REGEN = True
_PAGE_LOCKS: Dict[str, asyncio.Lock] = {}
_MANIFEST_LOCK: Optional[asyncio.Lock] = None


def set_static_image_config(config: Optional[dict]):
    """从插件配置读取自动重生成开关"""
    global _AUTO_REGEN
    if not isinstance(config, dict):
        return
    cards = config.get("IMAGE_CARDS", {})
    if isinstance(cards, dict) and "STATIC_AUTO_REGEN" in cards:
        _AUTO_REGEN = bool(cards.get("STATIC_AUTO_REGEN"))


def auto_regenerate_enabled() -> bool:
    return _AUTO_REGEN


def list_pages():
    return list(PAGES.keys())


def register_page(page_key: str, spec: Dict[str, Any]) -> bool:
    """注册运行时构建的固定页（如符箓配方），供生成器缓存与渲染。"""
    if not page_key or not isinstance(spec, dict) or not spec.get("sections"):
        return False
    PAGES[page_key] = spec
    return True


def image_path(page_key: str) -> Path:
    return OUTPUT_DIR / f"{page_key}.jpg"


def _page_lock(page_key: str) -> asyncio.Lock:
    if page_key not in _PAGE_LOCKS:
        _PAGE_LOCKS[page_key] = asyncio.Lock()
    return _PAGE_LOCKS[page_key]


def _manifest_lock() -> asyncio.Lock:
    global _MANIFEST_LOCK
    if _MANIFEST_LOCK is None:
        _MANIFEST_LOCK = asyncio.Lock()
    return _MANIFEST_LOCK


def read_manifest() -> Dict[str, Any]:
    try:
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _write_manifest(manifest: Dict[str, Any]):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST_PATH.with_name(MANIFEST_PATH.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MANIFEST_PATH)


def is_current(page_key: str) -> bool:
    path = image_path(page_key)
    if not path.exists():
        return False
    entry = read_manifest().get(page_key)
    return bool(entry and entry.get("hash") == page_fingerprint(page_key))


async def generate_page(page_key: str, force: bool = False) -> Optional[str]:
    """生成（或复用）指定页面图片，返回图片路径；失败返回已有图片或 None"""
    if page_key not in PAGES:
        raise KeyError(f"未注册的静态页面: {page_key}")

    async with _page_lock(page_key):
        path = image_path(page_key)
        if not force and is_current(page_key):
            return str(path)

        if not HAS_PIL:
            logger.warning(f"【修仙插件】缺少 Pillow，无法生成静态页图片: {page_key}")
            return str(path) if path.exists() else None

        spec = PAGES[page_key]
        if spec.get("renderer") == "help_columns":
            from .help_image import render_help_page_async
            temp_path = await render_help_page_async(
                spec.get("title", ""),
                spec.get("subtitle", ""),
                spec.get("sections", []),
                spec.get("footer", ""),
                spec.get("width", 1600),
            )
        else:
            temp_path = await render_card_async(
                spec.get("background", "default"),
                spec.get("title", ""),
                spec.get("subtitle", ""),
                spec.get("sections", []),
                spec.get("footer", ""),
                spec.get("width", 1000),
            )
        if not temp_path:
            logger.warning(f"【修仙插件】静态页图片渲染失败: {page_key}")
            return str(path) if path.exists() else None

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        tmp_target = path.with_name(path.name + ".tmp")
        try:
            shutil.copyfile(temp_path, tmp_target)
            os.replace(tmp_target, path)
        finally:
            Path(temp_path).unlink(missing_ok=True)
            Path(tmp_target).unlink(missing_ok=True)

        async with _manifest_lock():
            manifest = read_manifest()
            manifest[page_key] = {
                "hash": page_fingerprint(page_key),
                "file": path.name,
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            _write_manifest(manifest)
        logger.info(f"【修仙插件】静态页图片已生成: {path}")
        return str(path)


async def ensure_page(page_key: str, allow_generate: Optional[bool] = None) -> Optional[str]:
    """运行时取图：存在且未过期直接用；过期且允许时自动重生成，失败则用旧图"""
    if page_key not in PAGES:
        return None
    if allow_generate is None:
        allow_generate = _AUTO_REGEN

    path = image_path(page_key)
    if path.exists() and (not allow_generate or is_current(page_key)):
        return str(path)
    if allow_generate:
        generated = await generate_page(page_key, force=False)
        if generated:
            return generated
    return str(path) if path.exists() else None


async def generate_all(force: bool = False) -> Dict[str, Optional[str]]:
    results: Dict[str, Optional[str]] = {}
    for page_key in PAGES:
        results[page_key] = await generate_page(page_key, force=force)
    return results
