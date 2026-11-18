# utils/game_secret.py
"""全局随机密钥：让「赌局类」随机结果无法被玩家离线复现。

背景（与 ``managers/invest_manager.py`` 的行情密钥同一套思路）：
    盘口胜负、暗标拍品、矿脉富集度、赌石/锦囊的隐藏结果，过去都用
    ``random.Random(f"pool:{key}")`` 这类**公开可推算**的种子。插件开源、
    种子要素（小时/轮次/编号/自己的 user_id）玩家全知道，任何人写个脚本
    就能提前算出赢家，把高风险玩法变成无风险套利。

做法：
    首次启动随机生成一枚密钥落到 ``system_config.game_rng_secret``，
    所有赌局随机都改走 :func:`seeded_rng`，把密钥混进种子后再 md5。

兼容性：
    密钥尚未加载（例如离线单元测试直接构造 Manager）时，
    :func:`seeded_rng` 退回「直接用字符串当种子」的旧行为，
    因此已有测试的随机序列不会改变。
"""

from __future__ import annotations

import hashlib
import random
import secrets as _pysecrets
from typing import Optional

__all__ = ["ensure_game_secret", "game_secret", "seeded_rng", "reset_game_secret"]

CONFIG_KEY = "game_rng_secret"

_CACHE: dict = {"value": ""}


async def ensure_game_secret(db) -> str:
    """确保密钥已生成并缓存，返回密钥明文。插件初始化时调用一次即可。"""
    if _CACHE["value"]:
        return _CACHE["value"]
    stored: Optional[str] = None
    try:
        stored = await db.ext.get_system_config(CONFIG_KEY)
    except Exception:
        stored = None
    if not stored:
        stored = _pysecrets.token_hex(16)
        try:
            await db.ext.set_system_config(CONFIG_KEY, stored)
        except Exception:
            # 落库失败就只在本次进程内生效：下次重启会重新生成，
            # 宁可密钥变动也不要让赌局结果变成公开可推算。
            pass
    _CACHE["value"] = str(stored)
    return _CACHE["value"]


def game_secret() -> str:
    """同步读取已缓存的密钥；未加载时返回空串。"""
    return _CACHE["value"] or ""


def reset_game_secret(value: str = "") -> None:
    """测试钩子：手动设置/清空缓存密钥。"""
    _CACHE["value"] = str(value or "")


def seeded_rng(*parts) -> random.Random:
    """把若干要素混上服务器密钥后生成 Random。

    用法与旧的 ``random.Random(f"a:{b}:{c}")`` 等价：
        rng = seeded_rng("pool-result", pool_key)
    """
    raw = ":".join(str(p) for p in parts)
    secret = _CACHE["value"] or ""
    if not secret:
        return random.Random(raw)
    digest = hashlib.md5(f"{raw}:{secret}".encode("utf-8")).hexdigest()
    return random.Random(int(digest, 16))
