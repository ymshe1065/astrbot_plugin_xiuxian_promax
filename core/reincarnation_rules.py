# core/reincarnation_rules.py
"""飞升 / 弃道重修共用的永久加成账本。

背景：仙缘商店第 3 项【大道馈赠】卖的是「永久属性」，但原实现只改了
``player.mental_power`` 这类当世字段。而飞升与弃道重修都会用
``generate_new_player_stats()`` 把整组属性覆盖成新号基础值，于是花 500
仙缘点买的加成会在下一次转生时静默清零——与「永久」的承诺相悖。

解法：把加成同时记进 ``permanent_bonus["dao_gift"]``（按字段累加），
转生重置完成后调用 :func:`apply_dao_gift` 把账本重新加回属性。
账本只增不减，所以飞升 N 次就重放 N 次，不会重复叠加。
"""
from __future__ import annotations

import json
from typing import Any, Dict

# 单次兑换的固定加成：key 与 Player 属性名一一对应
DAO_GIFT_BONUS: Dict[str, int] = {
    "mental_power": 100,
    "max_spiritual_qi": 100,
    "max_blood_qi": 100,
    "magic_damage": 20,
    "physical_damage": 20,
    "magic_defense": 10,
    "physical_defense": 10,
}
DAO_GIFT_COST = 500

DAO_GIFT_LABELS: Dict[str, str] = {
    "mental_power": "精神",
    "max_spiritual_qi": "灵气上限",
    "max_blood_qi": "气血上限",
    "magic_damage": "法伤",
    "physical_damage": "物伤",
    "magic_defense": "法防",
    "physical_defense": "物防",
}


def parse_bonus(player: Any) -> Dict[str, Any]:
    """读取 permanent_bonus；脏数据一律当空账本，避免转生流程被一条坏 JSON 卡死。"""
    raw = getattr(player, "permanent_bonus", "") or ""
    try:
        data = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def dao_gift_ledger(bonus: Dict[str, Any]) -> Dict[str, int]:
    """取出大道馈赠账本（字段 → 累计加成），忽略非法值。"""
    raw = (bonus or {}).get("dao_gift")
    ledger: Dict[str, int] = {}
    if not isinstance(raw, dict):
        return ledger
    for key in DAO_GIFT_BONUS:
        try:
            value = int(raw.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value:
            ledger[key] = value
    return ledger


def dao_gift_times(bonus: Dict[str, Any]) -> int:
    """已兑换次数：按最小一项折算，避免账本被手改后显示成奇怪数字。"""
    ledger = dao_gift_ledger(bonus)
    if not ledger:
        return 0
    tiers = [
        ledger.get(key, 0) // step
        for key, step in DAO_GIFT_BONUS.items()
        if step > 0
    ]
    return min(tiers) if tiers else 0


def grant_dao_gift(player: Any, bonus: Dict[str, Any]) -> None:
    """兑换一次：当场加到属性上，同时记账，供下次转生重放。"""
    if not isinstance(bonus, dict):
        bonus = {}
    for key, step in DAO_GIFT_BONUS.items():
        try:
            current = int(getattr(player, key, 0) or 0)
        except (TypeError, ValueError):
            current = 0
        setattr(player, key, current + step)

    # 注意：不能写成 (bonus or {}).setdefault(...) —— 空 dict 会被换成临时对象，账本丢失
    ledger = bonus.get("dao_gift")
    if not isinstance(ledger, dict):
        ledger = {}
        bonus["dao_gift"] = ledger
    for key, step in DAO_GIFT_BONUS.items():
        try:
            current = int(ledger.get(key, 0) or 0)
        except (TypeError, ValueError):
            current = 0
        ledger[key] = current + step


def apply_dao_gift(player: Any, bonus: Dict[str, Any]) -> None:
    """转生重置后按账本重新加回属性。

    必须在 ``generate_new_player_stats()`` 覆盖属性之后调用，
    否则会被基础属性覆盖掉。
    """
    for key, value in dao_gift_ledger(bonus).items():
        try:
            current = int(getattr(player, key, 0) or 0)
        except (TypeError, ValueError):
            current = 0
        setattr(player, key, current + value)


def format_dao_gift(bonus: Dict[str, Any]) -> str:
    """一行文案：已兑换 N 次 · 精神+100 / …；没买过返回空串。"""
    ledger = dao_gift_ledger(bonus)
    if not ledger:
        return ""
    parts = [
        f"{DAO_GIFT_LABELS.get(key, key)}+{value:,}"
        for key, value in ledger.items()
        if value
    ]
    return f"大道馈赠 {dao_gift_times(bonus)} 次（{' / '.join(parts)}）"
